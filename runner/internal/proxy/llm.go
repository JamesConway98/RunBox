package proxy

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"path/filepath"
	"strings"
	"time"
)

// LLMProxy forwards the agent's provider calls upstream, injecting the API key.
//
// The agent never receives a credential. That is the point: the most valuable
// thing in the system is the provider key, and the least trusted thing in the
// system is the code running inside the sandbox. Putting the two in the same
// process would undo most of what the isolation buys.
type LLMProxy struct {
	listener net.Listener
	server   *http.Server
	log      *slog.Logger
}

// LLMConfig is the upstream and the credential to attach to it.
type LLMConfig struct {
	Upstream string // e.g. https://api.anthropic.com
	APIKey   string
	Version  string // anthropic-version header
}

// StartLLM begins serving on dir/llm.sock.
func StartLLM(dir string, cfg LLMConfig, log *slog.Logger) (*LLMProxy, error) {
	upstream, err := url.Parse(cfg.Upstream)
	if err != nil {
		return nil, err
	}

	listener, err := listenUnix(filepath.Join(dir, LLMSocketName))
	if err != nil {
		return nil, err
	}

	reverse := &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(upstream)
			r.Out.Host = upstream.Host

			// Strip anything the agent tried to send in an auth position, then
			// set our own. A compromised agent must not be able to make this
			// proxy forward a key of its choosing, or override the version and
			// get a response shape the runner cannot parse.
			r.Out.Header.Del("Authorization")
			r.Out.Header.Del("X-Api-Key")
			r.Out.Header.Del("Anthropic-Version")

			r.Out.Header.Set("x-api-key", cfg.APIKey)
			r.Out.Header.Set("anthropic-version", cfg.Version)
		},

		// Streaming is the whole reason this exists. Without an explicit flush
		// interval the reverse proxy buffers, and every token arrives at once
		// when the response completes.
		FlushInterval: -1,

		Transport: &http.Transport{
			Proxy:                 nil, // never honour HTTP_PROXY from the environment
			MaxIdleConnsPerHost:   4,
			IdleConnTimeout:       90 * time.Second,
			TLSHandshakeTimeout:   10 * time.Second,
			ResponseHeaderTimeout: 120 * time.Second,
			ExpectContinueTimeout: time.Second,
		},

		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			log.Warn("llm proxy upstream error", "err", err)
			w.WriteHeader(http.StatusBadGateway)
			_, _ = w.Write([]byte(`{"error":{"message":"upstream unreachable"}}`))
		},
	}

	server := &http.Server{
		Handler:           guardPaths(reverse, log),
		ReadHeaderTimeout: 10 * time.Second,
	}

	p := &LLMProxy{listener: listener, server: server, log: log}
	go func() {
		if err := server.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Warn("llm proxy stopped", "err", err)
		}
	}()
	return p, nil
}

// guardPaths restricts the proxy to the endpoints the agent actually uses.
//
// Without this, the socket is a general-purpose authenticated tunnel to the
// provider's whole API — including key management and billing endpoints, on a
// key the agent is not supposed to be able to see.
func guardPaths(next http.Handler, log *slog.Logger) http.Handler {
	allowed := map[string]bool{
		"/v1/messages": true,
	}

	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := strings.TrimSuffix(r.URL.Path, "/")
		if r.Method != http.MethodPost || !allowed[path] {
			log.Warn("llm proxy rejected request", "method", r.Method, "path", r.URL.Path)
			w.WriteHeader(http.StatusForbidden)
			_, _ = w.Write([]byte(`{"error":{"message":"endpoint not permitted"}}`))
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (p *LLMProxy) Close() error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return p.server.Shutdown(ctx)
}
