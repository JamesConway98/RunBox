package proxy

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"path/filepath"
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

// Upstream is one provider the sandbox may reach, and the credential to attach.
type Upstream struct {
	// Path is the single endpoint this upstream exposes. Routing by path keeps
	// the socket from becoming a general tunnel to anybody's whole API.
	Path    string
	BaseURL string
	APIKey  string
	// Header names the auth scheme. Anthropic wants x-api-key, everyone
	// OpenAI-compatible wants Authorization: Bearer.
	Header string
	Prefix string            // e.g. "Bearer " — empty for a bare key
	Extra  map[string]string // provider-specific headers, e.g. anthropic-version
}

// LLMConfig is the set of providers reachable from one run's sandbox.
type LLMConfig struct {
	Upstreams []Upstream
}

// AnthropicUpstream is the default provider wiring.
func AnthropicUpstream(baseURL, apiKey, version string) Upstream {
	return Upstream{
		Path:    "/v1/messages",
		BaseURL: baseURL,
		APIKey:  apiKey,
		Header:  "x-api-key",
		Extra:   map[string]string{"anthropic-version": version},
	}
}

// OpenAIUpstream covers OpenAI and every gateway that speaks its shape.
func OpenAIUpstream(baseURL, apiKey string) Upstream {
	return Upstream{
		Path:    "/v1/chat/completions",
		BaseURL: baseURL,
		APIKey:  apiKey,
		Header:  "Authorization",
		Prefix:  "Bearer ",
	}
}

// StartLLM begins serving on dir/llm.sock.
func StartLLM(dir string, cfg LLMConfig, log *slog.Logger) (*LLMProxy, error) {
	mux := http.NewServeMux()
	routed := 0

	for _, up := range cfg.Upstreams {
		// A provider with no key configured is simply not mounted. Mounting it
		// would mean the agent gets a confusing 401 from upstream instead of a
		// clear "not permitted" from us.
		if up.APIKey == "" || up.BaseURL == "" {
			log.Debug("llm proxy: upstream not configured, skipping", "path", up.Path)
			continue
		}
		handler, err := reverseTo(up, log)
		if err != nil {
			return nil, err
		}
		mux.Handle(up.Path, handler)
		routed++
	}

	if routed == 0 {
		return nil, fmt.Errorf("llm proxy: no upstreams configured")
	}

	listener, err := listenUnix(filepath.Join(dir, LLMSocketName))
	if err != nil {
		return nil, err
	}

	server := &http.Server{
		// Anything not explicitly mounted is refused. Without that, the socket
		// is a general-purpose authenticated tunnel to a provider's whole API —
		// key management and billing included — on a key the agent is not
		// supposed to be able to see.
		Handler:           refuseUnrouted(mux, log),
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

func reverseTo(up Upstream, log *slog.Logger) (http.Handler, error) {
	target, err := url.Parse(up.BaseURL)
	if err != nil {
		return nil, fmt.Errorf("parse upstream %q: %w", up.BaseURL, err)
	}

	return &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(target)
			r.Out.Host = target.Host

			// Strip everything the agent might have put in an auth position
			// before setting our own. A compromised agent must not be able to
			// make this proxy forward a key of its choosing, or downgrade the
			// API version to get a response shape the runner cannot parse.
			r.Out.Header.Del("Authorization")
			r.Out.Header.Del("X-Api-Key")
			r.Out.Header.Del("Anthropic-Version")
			r.Out.Header.Del("OpenAI-Organization")

			r.Out.Header.Set(up.Header, up.Prefix+up.APIKey)
			for name, value := range up.Extra {
				r.Out.Header.Set(name, value)
			}
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
	}, nil
}

func refuseUnrouted(mux *http.ServeMux, log *slog.Logger) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		handler, pattern := mux.Handler(r)
		if pattern == "" || r.Method != http.MethodPost {
			log.Warn("llm proxy rejected request", "method", r.Method, "path", r.URL.Path)
			w.WriteHeader(http.StatusForbidden)
			_, _ = w.Write([]byte(`{"error":{"message":"endpoint not permitted"}}`))
			return
		}
		handler.ServeHTTP(w, r)
	})
}

func (p *LLMProxy) Close() error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return p.server.Shutdown(ctx)
}
