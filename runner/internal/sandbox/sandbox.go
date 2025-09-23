// Package sandbox owns the container lifecycle for a single run.
//
// One container per run, created with tight defaults, streamed line by line,
// and removed unconditionally when the run ends — including when it ends
// because the context was cancelled.
package sandbox

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/docker/docker/api/types/container"
	"github.com/docker/docker/api/types/mount"
	"github.com/docker/docker/api/types/network"
	"github.com/docker/docker/client"
	"github.com/docker/docker/pkg/stdcopy"
)

// Spec is everything needed to start one agent container.
type Spec struct {
	RunID        string
	Task         string
	Model        string
	Tools        []string
	SystemPrompt string
	Temperature  *float64
	MaxTokens    int

	Image     string
	MemoryMB  int64
	CPUs      float64
	PidsLimit int64

	// Env holds values injected by the runner. These never come from the API
	// caller — a tenant cannot make the sandbox carry an environment variable.
	//
	// Notably absent: the provider API key. The agent reaches the model through
	// the runner's LLM proxy, so the most valuable credential in the system is
	// never present in the least trusted process in the system.
	Env map[string]string

	// SocketDir is the host directory holding this run's proxy sockets. It is
	// bind-mounted at /run/runbox and is the container's only route out.
	SocketDir string
}

// Limits describes what was actually applied, for logging and for the README's
// honesty about what this sandbox is and is not.
type Limits struct {
	Network     string
	ReadonlyFS  bool
	CapDrop     []string
	MemoryBytes int64
	NanoCPUs    int64
	PidsLimit   int64
}

type Sandbox struct {
	cli *client.Client
	log *slog.Logger
}

func New(log *slog.Logger) (*Sandbox, error) {
	cli, err := client.NewClientWithOpts(client.FromEnv, client.WithAPIVersionNegotiation())
	if err != nil {
		return nil, fmt.Errorf("docker client: %w", err)
	}
	return &Sandbox{cli: cli, log: log}, nil
}

func (s *Sandbox) Close() error { return s.cli.Close() }

// Ping verifies the Docker socket is reachable. Called at boot so that a
// missing socket is a startup failure rather than a mystery on the first run.
func (s *Sandbox) Ping(ctx context.Context) error {
	if _, err := s.cli.Ping(ctx); err != nil {
		return fmt.Errorf("docker ping: %w", err)
	}
	return nil
}

// Container is a started sandbox, ready to be streamed.
type Container struct {
	ID     string
	Limits Limits

	sandbox *Sandbox
}

// Start creates and starts the container. The caller must always call Remove,
// including on error paths, which is why Start returns a Container rather than
// taking a callback: defer belongs at the call site where the context lives.
func (s *Sandbox) Start(ctx context.Context, spec Spec) (*Container, error) {
	env := []string{
		"RUNBOX_TASK=" + spec.Task,
		"RUNBOX_MODEL=" + spec.Model,
		"RUNBOX_TOOLS=" + strings.Join(spec.Tools, ","),
		"RUNBOX_MAX_TOKENS=" + strconv.Itoa(spec.MaxTokens),
	}
	if spec.SystemPrompt != "" {
		env = append(env, "RUNBOX_SYSTEM_PROMPT="+spec.SystemPrompt)
	}
	if spec.Temperature != nil {
		env = append(env, "RUNBOX_TEMPERATURE="+strconv.FormatFloat(*spec.Temperature, 'f', -1, 64))
	}
	for k, v := range spec.Env {
		env = append(env, k+"="+v)
	}

	limits := Limits{
		// No route to anywhere. The agent's model calls and its http_get tool
		// both go through unix sockets the runner controls, so this is a real
		// setting rather than one that gets reverted the first time the agent
		// needs an API.
		Network:     "none",
		ReadonlyFS:  true,
		CapDrop:     []string{"ALL"},
		MemoryBytes: spec.MemoryMB * 1024 * 1024,
		NanoCPUs:    int64(spec.CPUs * 1e9),
		PidsLimit:   spec.PidsLimit,
	}

	mounts := []mount.Mount{}
	if spec.SocketDir != "" {
		mounts = append(mounts, mount.Mount{
			Type:   mount.TypeBind,
			Source: spec.SocketDir,
			Target: "/run/runbox",
			// Not read-only: a unix socket needs write access to be connected
			// to. The directory contains nothing but sockets, and the container
			// cannot create anything useful in it.
			ReadOnly: false,
		})
	}

	hostCfg := &container.HostConfig{
		NetworkMode:    container.NetworkMode(limits.Network),
		ReadonlyRootfs: limits.ReadonlyFS,
		CapDrop:        limits.CapDrop,
		SecurityOpt:    []string{"no-new-privileges"},
		Mounts:         mounts,
		// A small writable tmpfs, mounted noexec so a downloaded payload cannot
		// be made runnable.
		Tmpfs: map[string]string{
			"/tmp": "rw,noexec,nosuid,nodev,size=64m",
		},
		Resources: container.Resources{
			Memory:    limits.MemoryBytes,
			NanoCPUs:  limits.NanoCPUs,
			PidsLimit: &limits.PidsLimit,
		},
		AutoRemove:    false, // we remove explicitly, after reading the exit code
		RestartPolicy: container.RestartPolicy{Name: "no"},
	}

	created, err := s.cli.ContainerCreate(
		ctx,
		&container.Config{
			Image:           spec.Image,
			Env:             env,
			NetworkDisabled: true,
			Labels: map[string]string{
				"runbox.run_id":  spec.RunID,
				"runbox.managed": "true",
			},
		},
		hostCfg,
		&network.NetworkingConfig{},
		nil,
		"runbox-"+spec.RunID,
	)
	if err != nil {
		return nil, fmt.Errorf("create container: %w", err)
	}

	if err := s.cli.ContainerStart(ctx, created.ID, container.StartOptions{}); err != nil {
		// Best-effort cleanup: a created-but-unstarted container is still a
		// leak, and this is the failure path that produces them.
		s.remove(context.WithoutCancel(ctx), created.ID)
		return nil, fmt.Errorf("start container: %w", err)
	}

	s.log.Debug("container started",
		"run_id", spec.RunID, "container", created.ID[:12], "image", spec.Image)

	return &Container{ID: created.ID, Limits: limits, sandbox: s}, nil
}

// Stream calls onLine for every line the agent writes to stdout, until the
// container exits or ctx is cancelled.
//
// Docker multiplexes stdout and stderr on one connection; stdcopy demuxes it.
// stderr is forwarded to onStderr rather than dropped, because a Python
// traceback is the single most useful thing to have when a run fails.
func (c *Container) Stream(
	ctx context.Context,
	onLine func([]byte) error,
	onStderr func(string),
) error {
	logs, err := c.sandbox.cli.ContainerLogs(ctx, c.ID, container.LogsOptions{
		ShowStdout: true,
		ShowStderr: true,
		Follow:     true,
		Timestamps: false,
	})
	if err != nil {
		return fmt.Errorf("attach logs: %w", err)
	}
	defer logs.Close()

	stdoutR, stdoutW := io.Pipe()
	stderrR, stderrW := io.Pipe()

	go func() {
		_, copyErr := stdcopy.StdCopy(stdoutW, stderrW, logs)
		stdoutW.CloseWithError(copyErr)
		stderrW.CloseWithError(copyErr)
	}()

	stderrDone := make(chan struct{})
	go func() {
		defer close(stderrDone)
		scanner := bufio.NewScanner(stderrR)
		scanner.Buffer(make([]byte, 0, 64*1024), 1024*1024)
		for scanner.Scan() {
			if line := strings.TrimSpace(scanner.Text()); line != "" && onStderr != nil {
				onStderr(line)
			}
		}
		io.Copy(io.Discard, stderrR) //nolint:errcheck // drain so the pipe never blocks
	}()

	scanner := bufio.NewScanner(stdoutR)
	// A tool_result event can carry 24k of page text. The default 64k token
	// limit is close enough to that to be worth raising deliberately.
	scanner.Buffer(make([]byte, 0, 128*1024), 4*1024*1024)

	var scanErr error
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(strings.TrimSpace(string(line))) == 0 {
			continue
		}
		if err := onLine(line); err != nil {
			scanErr = err
			break
		}
		if ctx.Err() != nil {
			scanErr = ctx.Err()
			break
		}
	}
	if scanErr == nil {
		scanErr = scanner.Err()
	}

	stdoutR.CloseWithError(scanErr)
	<-stderrDone

	if scanErr != nil && !errors.Is(scanErr, io.EOF) {
		return scanErr
	}
	return nil
}

// Wait blocks until the container exits and returns its exit code.
func (c *Container) Wait(ctx context.Context) (int64, error) {
	statusCh, errCh := c.sandbox.cli.ContainerWait(ctx, c.ID, container.WaitConditionNotRunning)
	select {
	case err := <-errCh:
		return -1, fmt.Errorf("wait: %w", err)
	case status := <-statusCh:
		if status.Error != nil {
			return status.StatusCode, fmt.Errorf("container error: %s", status.Error.Message)
		}
		return status.StatusCode, nil
	case <-ctx.Done():
		return -1, ctx.Err()
	}
}

// Kill sends SIGTERM and gives the agent a grace period to emit a final event
// before the daemon escalates to SIGKILL. Cooperative cancellation is only
// cooperative if the process is actually given a moment to cooperate.
func (c *Container) Kill(grace time.Duration) error {
	ctx, cancel := context.WithTimeout(context.Background(), grace+5*time.Second)
	defer cancel()

	seconds := int(grace.Seconds())
	if err := c.sandbox.cli.ContainerStop(ctx, c.ID, container.StopOptions{Timeout: &seconds}); err != nil {
		return fmt.Errorf("stop container: %w", err)
	}
	return nil
}

// Remove deletes the container. Always called, never conditional.
func (c *Container) Remove() {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	c.sandbox.remove(ctx, c.ID)
}

func (s *Sandbox) remove(ctx context.Context, id string) {
	err := s.cli.ContainerRemove(ctx, id, container.RemoveOptions{
		Force:         true,
		RemoveVolumes: true,
	})
	if err != nil && !client.IsErrNotFound(err) {
		s.log.Warn("container removal failed", "container", id, "err", err)
	}
}
