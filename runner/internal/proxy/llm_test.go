package proxy

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// clientOver returns an HTTP client that reaches the proxy over its unix
// socket, the same way the agent does.
func clientOver(dir string) *http.Client {
	socket := filepath.Join(dir, LLMSocketName)
	return &http.Client{
		Timeout: 5 * time.Second,
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "unix", socket)
			},
		},
	}
}

func startLLM(t *testing.T, upstream string, key string) string {
	t.Helper()
	dir := shortTempDir(t)
	p, err := StartLLM(dir, LLMConfig{
		Upstream: upstream,
		APIKey:   key,
		Version:  "2023-06-01",
	}, quietLogger())
	if err != nil {
		t.Fatalf("StartLLM: %v", err)
	}
	t.Cleanup(func() { p.Close() })
	return dir
}

func TestInjectsAPIKeyUpstream(t *testing.T) {
	var seenKey, seenVersion string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenKey = r.Header.Get("x-api-key")
		seenVersion = r.Header.Get("anthropic-version")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer upstream.Close()

	dir := startLLM(t, upstream.URL, "sk-real-secret")
	resp, err := clientOver(dir).Post(
		"http://llm.runbox.internal/v1/messages", "application/json", strings.NewReader("{}"),
	)
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	defer resp.Body.Close()

	if seenKey != "sk-real-secret" {
		t.Errorf("upstream saw key %q", seenKey)
	}
	if seenVersion != "2023-06-01" {
		t.Errorf("upstream saw version %q", seenVersion)
	}
}

func TestAgentSuppliedCredentialsAreStripped(t *testing.T) {
	// A compromised agent must not be able to make the proxy forward a key of
	// its choosing, or downgrade the API version to get a response shape the
	// runner cannot parse.
	var seenKey, seenAuth, seenVersion string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenKey = r.Header.Get("x-api-key")
		seenAuth = r.Header.Get("Authorization")
		seenVersion = r.Header.Get("anthropic-version")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer upstream.Close()

	dir := startLLM(t, upstream.URL, "sk-real-secret")

	req, _ := http.NewRequest(
		http.MethodPost, "http://llm.runbox.internal/v1/messages", strings.NewReader("{}"),
	)
	req.Header.Set("x-api-key", "sk-attacker-key")
	req.Header.Set("Authorization", "Bearer sk-attacker-token")
	req.Header.Set("anthropic-version", "1999-01-01")

	resp, err := clientOver(dir).Do(req)
	if err != nil {
		t.Fatalf("do: %v", err)
	}
	defer resp.Body.Close()

	if seenKey != "sk-real-secret" {
		t.Errorf("agent's key reached upstream: %q", seenKey)
	}
	if seenAuth != "" {
		t.Errorf("Authorization header was forwarded: %q", seenAuth)
	}
	if seenVersion != "2023-06-01" {
		t.Errorf("agent overrode the API version: %q", seenVersion)
	}
}

func TestOnlyMessagesEndpointIsReachable(t *testing.T) {
	// Without a path guard the socket is an authenticated tunnel to the
	// provider's entire API, on a key the agent is not meant to hold.
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("upstream should not have been reached")
	}))
	defer upstream.Close()

	dir := startLLM(t, upstream.URL, "sk-real-secret")
	client := clientOver(dir)

	for _, path := range []string{"/v1/api_keys", "/v1/organizations", "/v1/models", "/"} {
		t.Run(path, func(t *testing.T) {
			resp, err := client.Post(
				"http://llm.runbox.internal"+path, "application/json", strings.NewReader("{}"),
			)
			if err != nil {
				t.Fatalf("post: %v", err)
			}
			defer resp.Body.Close()

			if resp.StatusCode != http.StatusForbidden {
				t.Errorf("%s returned %d, want 403", path, resp.StatusCode)
			}
		})
	}
}

func TestNonPostIsRejected(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("upstream should not have been reached")
	}))
	defer upstream.Close()

	dir := startLLM(t, upstream.URL, "sk-real-secret")

	resp, err := clientOver(dir).Get("http://llm.runbox.internal/v1/messages")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusForbidden {
		t.Errorf("GET returned %d, want 403", resp.StatusCode)
	}
}

func TestUpstreamFailureBecomesBadGateway(t *testing.T) {
	// Closed immediately, so the proxy has nowhere to forward to.
	upstream := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
	upstream.Close()

	dir := startLLM(t, upstream.URL, "sk-real-secret")

	resp, err := clientOver(dir).Post(
		"http://llm.runbox.internal/v1/messages", "application/json", strings.NewReader("{}"),
	)
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusBadGateway {
		t.Errorf("status = %d, want 502", resp.StatusCode)
	}
	body, _ := io.ReadAll(resp.Body)
	// The agent needs a parseable error, not an empty body it will treat as a
	// malformed stream and retry against.
	if !strings.Contains(string(body), "upstream unreachable") {
		t.Errorf("body = %q", body)
	}
}
