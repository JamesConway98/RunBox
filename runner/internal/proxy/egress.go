package proxy

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	maxBodyBytes   = 512 * 1024
	requestTimeout = 30 * time.Second
	maxRedirects   = 3
)

// EgressProxy serves the http_get tool over a unix socket, enforcing a host
// allowlist.
//
// A line of JSON in, a line of JSON out. The protocol is deliberately not HTTP:
// if the agent could speak HTTP to this socket it could try to make it proxy
// arbitrary methods and headers, and the surface for confusing the fetcher
// grows. A request that can only express "fetch this URL" is a request that
// cannot express anything else.
type EgressProxy struct {
	listener  net.Listener
	client    *http.Client
	allowlist map[string]bool
	log       *slog.Logger

	wg     sync.WaitGroup
	closed chan struct{}
}

type egressRequest struct {
	URL string `json:"url"`
}

type egressResponse struct {
	Status int    `json:"status,omitempty"`
	Body   string `json:"body,omitempty"`
	Error  string `json:"error,omitempty"`
}

func StartEgress(dir string, allowlist []string, log *slog.Logger) (*EgressProxy, error) {
	listener, err := listenUnix(filepath.Join(dir, EgressSocketName))
	if err != nil {
		return nil, err
	}

	allowed := make(map[string]bool, len(allowlist))
	for _, host := range allowlist {
		allowed[strings.ToLower(strings.TrimSpace(host))] = true
	}

	p := &EgressProxy{
		listener:  listener,
		allowlist: allowed,
		log:       log,
		closed:    make(chan struct{}),
		client: &http.Client{
			Timeout: requestTimeout,
			CheckRedirect: func(req *http.Request, via []*http.Request) error {
				// Redirects are the obvious way around an allowlist: request an
				// allowed host, have it 302 you anywhere. Every hop is checked.
				if len(via) >= maxRedirects {
					return fmt.Errorf("too many redirects")
				}
				if !allowed[strings.ToLower(req.URL.Hostname())] {
					return fmt.Errorf("redirect to non-allowlisted host %q", req.URL.Hostname())
				}
				return nil
			},
		},
	}

	go p.serve()
	return p, nil
}

func (p *EgressProxy) serve() {
	for {
		conn, err := p.listener.Accept()
		if err != nil {
			select {
			case <-p.closed:
				return
			default:
				p.log.Warn("egress accept failed", "err", err)
				return
			}
		}
		p.wg.Add(1)
		go func() {
			defer p.wg.Done()
			p.handle(conn)
		}()
	}
}

func (p *EgressProxy) handle(conn net.Conn) {
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(requestTimeout + 5*time.Second))

	reader := bufio.NewReader(io.LimitReader(conn, 8*1024))
	line, err := reader.ReadBytes('\n')
	if err != nil && len(line) == 0 {
		return
	}

	var req egressRequest
	if err := json.Unmarshal(line, &req); err != nil {
		p.reply(conn, egressResponse{Error: "malformed request"})
		return
	}

	body, status, err := p.fetch(req.URL)
	if err != nil {
		p.reply(conn, egressResponse{Error: err.Error()})
		return
	}
	p.reply(conn, egressResponse{Status: status, Body: body})
}

func (p *EgressProxy) fetch(raw string) (string, int, error) {
	target, err := url.Parse(raw)
	if err != nil {
		return "", 0, fmt.Errorf("invalid url")
	}
	if target.Scheme != "http" && target.Scheme != "https" {
		return "", 0, fmt.Errorf("only http and https are supported")
	}

	host := strings.ToLower(target.Hostname())
	if !p.allowlist[host] {
		// The message names the host but not the allowlist. Telling the agent
		// what it *could* reach turns a denial into a directory.
		return "", 0, fmt.Errorf("host %q is not on the egress allowlist", host)
	}

	request, err := http.NewRequest(http.MethodGet, target.String(), nil)
	if err != nil {
		return "", 0, fmt.Errorf("could not build request")
	}
	request.Header.Set("User-Agent", "runbox-agent/0.1 (+https://github.com/JamesConway98/RunBox)")
	request.Header.Set("Accept", "text/plain, text/html, application/json;q=0.9, */*;q=0.5")

	response, err := p.client.Do(request)
	if err != nil {
		return "", 0, fmt.Errorf("request failed: %w", err)
	}
	defer response.Body.Close()

	// Bounded read. An agent asking for a 2GB file should not be able to make
	// the runner hold it in memory.
	content, err := io.ReadAll(io.LimitReader(response.Body, maxBodyBytes))
	if err != nil {
		return "", response.StatusCode, fmt.Errorf("read failed: %w", err)
	}
	return string(content), response.StatusCode, nil
}

func (p *EgressProxy) reply(conn net.Conn, resp egressResponse) {
	payload, err := json.Marshal(resp)
	if err != nil {
		payload = []byte(`{"error":"could not encode response"}`)
	}
	if _, err := conn.Write(append(payload, '\n')); err != nil {
		p.log.Debug("egress reply failed", "err", err)
	}
}

func (p *EgressProxy) Close() error {
	close(p.closed)
	err := p.listener.Close()
	p.wg.Wait()
	return err
}
