package proxy

import (
	"bufio"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// ask sends one request over the egress socket and returns the decoded reply.
func ask(t *testing.T, dir, target string) egressResponse {
	t.Helper()

	conn, err := net.Dial("unix", filepath.Join(dir, EgressSocketName))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()

	request, _ := json.Marshal(egressRequest{URL: target})
	if _, err := conn.Write(append(request, '\n')); err != nil {
		t.Fatalf("write: %v", err)
	}

	line, err := bufio.NewReader(conn).ReadBytes('\n')
	if err != nil && len(line) == 0 {
		t.Fatalf("read: %v", err)
	}

	var resp egressResponse
	if err := json.Unmarshal(line, &resp); err != nil {
		t.Fatalf("decode %q: %v", line, err)
	}
	return resp
}

// shortTempDir returns a temp dir with a deliberately short path.
//
// t.TempDir() embeds the test name, and a unix socket path is capped at ~104
// bytes on macOS and 108 on Linux. A long test name silently turns into
// "bind: invalid argument", which is not an obvious diagnosis.
func shortTempDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "rb")
	if err != nil {
		t.Fatalf("temp dir: %v", err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return dir
}

func startFor(t *testing.T, allowlist []string) string {
	t.Helper()
	dir := shortTempDir(t)
	proxy, err := StartEgress(dir, allowlist, quietLogger())
	if err != nil {
		t.Fatalf("StartEgress: %v", err)
	}
	t.Cleanup(func() { proxy.Close() })
	return dir
}

func TestFetchesAllowlistedHost(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("hello from upstream"))
	}))
	defer upstream.Close()

	host := mustHost(t, upstream.URL)
	dir := startFor(t, []string{host})

	resp := ask(t, dir, upstream.URL)

	if resp.Error != "" {
		t.Fatalf("unexpected error: %s", resp.Error)
	}
	if resp.Status != 200 || resp.Body != "hello from upstream" {
		t.Errorf("resp = %+v", resp)
	}
}

func TestRejectsHostNotOnAllowlist(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		t.Error("upstream should never have been contacted")
	}))
	defer upstream.Close()

	dir := startFor(t, []string{"example.com"})

	resp := ask(t, dir, upstream.URL)

	if resp.Error == "" {
		t.Fatal("expected a rejection")
	}
	if !strings.Contains(resp.Error, "not on the egress allowlist") {
		t.Errorf("error = %q", resp.Error)
	}
	// The denial must not enumerate what *is* allowed — that turns a rejection
	// into a directory of reachable hosts.
	if strings.Contains(resp.Error, "example.com") {
		t.Errorf("rejection leaked the allowlist: %q", resp.Error)
	}
}

func TestRedirectToNonAllowlistedHostIsBlocked(t *testing.T) {
	// The obvious way around an allowlist: request a permitted host and have it
	// redirect you somewhere else.
	//
	// httptest always binds 127.0.0.1, so the two servers need textually
	// different hostnames to be distinguishable by an allowlist that works on
	// names. "localhost" and "127.0.0.1" both resolve here but are different
	// strings, which is exactly the distinction the allowlist makes.
	forbidden := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("redirect target should never have been reached")
	}))
	defer forbidden.Close()

	allowed := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, forbidden.URL, http.StatusFound)
	}))
	defer allowed.Close()

	entry := strings.Replace(allowed.URL, "127.0.0.1", "localhost", 1)
	dir := startFor(t, []string{"localhost"})

	resp := ask(t, dir, entry)

	if resp.Error == "" {
		t.Fatalf("redirect escaped the allowlist: %+v", resp)
	}
	if !strings.Contains(resp.Error, "redirect") {
		t.Errorf("error should name the redirect: %q", resp.Error)
	}
}

func TestRejectsNonHTTPSchemes(t *testing.T) {
	dir := startFor(t, []string{"example.com"})

	for _, target := range []string{
		"file:///etc/passwd",
		"ftp://example.com/x",
		"gopher://example.com",
	} {
		t.Run(target, func(t *testing.T) {
			resp := ask(t, dir, target)
			if resp.Error == "" {
				t.Fatalf("scheme was accepted: %+v", resp)
			}
		})
	}
}

func TestRejectsMalformedRequest(t *testing.T) {
	dir := startFor(t, []string{"example.com"})

	conn, err := net.Dial("unix", filepath.Join(dir, EgressSocketName))
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()

	if _, err := conn.Write([]byte("this is not json\n")); err != nil {
		t.Fatalf("write: %v", err)
	}

	line, _ := bufio.NewReader(conn).ReadBytes('\n')
	var resp egressResponse
	if err := json.Unmarshal(line, &resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if resp.Error != "malformed request" {
		t.Errorf("error = %q", resp.Error)
	}
}

func TestBodyIsTruncatedRatherThanHeldInFull(t *testing.T) {
	// An agent asking for a huge file must not make the runner buffer it all.
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		chunk := strings.Repeat("x", 64*1024)
		for i := 0; i < 32; i++ { // 2 MB, well past the 512 KB cap
			_, _ = w.Write([]byte(chunk))
		}
	}))
	defer upstream.Close()

	dir := startFor(t, []string{mustHost(t, upstream.URL)})

	resp := ask(t, dir, upstream.URL)

	if resp.Error != "" {
		t.Fatalf("unexpected error: %s", resp.Error)
	}
	if len(resp.Body) > maxBodyBytes {
		t.Errorf("body was %d bytes, cap is %d", len(resp.Body), maxBodyBytes)
	}
}

func TestAllowlistIsCaseInsensitive(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("ok"))
	}))
	defer upstream.Close()

	host := mustHost(t, upstream.URL)
	dir := startFor(t, []string{strings.ToUpper(host)})

	if resp := ask(t, dir, upstream.URL); resp.Error != "" {
		t.Errorf("case-differing host was rejected: %s", resp.Error)
	}
}

func mustHost(t *testing.T, raw string) string {
	t.Helper()
	parsed, err := url.Parse(raw)
	if err != nil {
		t.Fatalf("parse %q: %v", raw, err)
	}
	return parsed.Hostname()
}
