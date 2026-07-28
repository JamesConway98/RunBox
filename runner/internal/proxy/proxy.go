// Package proxy gives a network-isolated container the two kinds of egress it
// legitimately needs, and nothing else.
//
// The sandbox runs with --network=none. It has no route to anywhere. Both
// proxies below listen on unix sockets that are bind-mounted into the
// container, so the only way out is through code the runner controls:
//
//   - llm.sock     an HTTP reverse proxy to the model provider. The runner
//     holds the API key and injects it, so a compromised agent
//     cannot exfiltrate a credential it never had.
//   - egress.sock  a line-protocol fetcher for the http_get tool, with a host
//     allowlist.
//
// This is what makes "--network=none" an honest claim rather than a setting
// that gets quietly reverted the first time the agent needs to reach an API.
package proxy

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
)

// SocketDir is where a run's sockets live inside the container.
const SocketDir = "/run/runbox"

const (
	LLMSocketName    = "llm.sock"
	EgressSocketName = "egress.sock"
)

// listenUnix creates a unix socket the container's unprivileged user can talk
// to.
//
// The 0666 mode is deliberate and worth explaining rather than apologising for.
// The socket lives in a per-run directory on the host that only the runner can
// reach, and it is bind-mounted into exactly one container. Matching uids
// across a container boundary is fragile — the image could be rebuilt with a
// different user — and the isolation here comes from the mount, not from the
// file mode.
func listenUnix(path string) (net.Listener, error) {
	// A socket left behind by a crashed run would make Listen fail with
	// "address already in use" forever.
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("clear stale socket: %w", err)
	}

	listener, err := net.Listen("unix", path)
	if err != nil {
		return nil, fmt.Errorf("listen on %s: %w", path, err)
	}
	if err := os.Chmod(path, 0o666); err != nil {
		listener.Close()
		return nil, fmt.Errorf("chmod socket: %w", err)
	}
	return listener, nil
}

// RunDir creates the host-side directory holding one run's sockets.
//
// 0711, not 0700. Connecting to a unix socket requires write permission on the
// socket and *traverse* permission on its directory, and the agent runs as an
// unrelated uid inside the container. With 0700 the socket was reachable only
// in theory: every run failed with "[Errno 13] Permission denied" before it
// reached the model.
//
// 0711 grants traverse without read, so the container can open a socket whose
// name it already knows but cannot enumerate the directory. The real isolation
// is one level up — the base directory is 0700 and owned by the runner, so
// nothing else on the host can reach these paths at all. Inside the container
// only this directory is mounted, and it contains nothing but its own sockets.
func RunDir(base, runID string) (string, error) {
	dir := filepath.Join(base, runID)
	if err := os.MkdirAll(dir, 0o711); err != nil {
		return "", fmt.Errorf("create socket dir: %w", err)
	}
	// MkdirAll honours the umask, which on a default Debian install strips the
	// group and other bits and puts us straight back to 0700. Set the mode
	// explicitly so the result does not depend on the runner's environment.
	if err := os.Chmod(dir, 0o711); err != nil {
		return "", fmt.Errorf("chmod socket dir: %w", err)
	}
	return dir, nil
}

// CleanupRunDir removes a run's socket directory. Always called, because a
// leaked directory per run is a slow-motion disk leak.
func CleanupRunDir(dir string) error {
	return os.RemoveAll(dir)
}
