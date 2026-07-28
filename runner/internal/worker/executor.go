// Package worker executes runs: container lifecycle, trace persistence and
// terminal state.
package worker

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"path/filepath"
	"strings"
	"time"

	"github.com/JamesConway98/RunBox/runner/internal/config"
	"github.com/JamesConway98/RunBox/runner/internal/proxy"
	"github.com/JamesConway98/RunBox/runner/internal/sandbox"
	"github.com/JamesConway98/RunBox/runner/internal/store"
	"github.com/JamesConway98/RunBox/runner/internal/trace"
)

// Publisher fans a stamped event out to live subscribers. In M1 this is a
// no-op; the SSE path arrives with Redis pub/sub.
type Publisher interface {
	Publish(ctx context.Context, runID string, event trace.Event) error
}

// KeyTaker hands over the caller-supplied provider key for a run, removing it
// as it does. Separate from Publisher so the executor's dependency on Redis is
// stated as two narrow capabilities rather than one broad client.
type KeyTaker interface {
	TakeProviderKey(ctx context.Context, runID string) (string, error)
}

type noopPublisher struct{}

func (noopPublisher) Publish(context.Context, string, trace.Event) error { return nil }

// Executor runs a single run to completion. One is shared across all workers;
// it holds no per-run state.
type Executor struct {
	cfg   *config.Config
	store *store.Store
	sbx   *sandbox.Sandbox
	pub   Publisher
	keys  KeyTaker
	log   *slog.Logger
}

func NewExecutor(
	cfg *config.Config, st *store.Store, sbx *sandbox.Sandbox,
	pub Publisher, keys KeyTaker, log *slog.Logger,
) *Executor {
	if pub == nil {
		pub = noopPublisher{}
	}
	return &Executor{cfg: cfg, store: st, sbx: sbx, pub: pub, keys: keys, log: log}
}

// Grace given to the agent between SIGTERM and SIGKILL, so a cancelled run can
// still emit its final event.
const killGrace = 3 * time.Second

// Execute runs one job start to finish. It does not return an error for a run
// that failed — a failed run is a successfully executed job whose terminal
// status happens to be "failed". An error here means the runner itself broke.
func (e *Executor) Execute(ctx context.Context, run *store.Run) error {
	started := time.Now()

	timeout := time.Duration(run.TimeoutS) * time.Second
	if timeout <= 0 {
		timeout = e.cfg.DefaultTimeout
	}
	if timeout > e.cfg.MaxTimeout {
		timeout = e.cfg.MaxTimeout
	}

	runCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	log := e.log.With("run_id", run.ID, "model", run.Model)
	log.Info("run started", "timeout", timeout, "tools", run.Tools)

	state := &runState{run: run, exec: e, log: log}
	state.buf = newTraceBuffer(run.ID, run.TenantID, e.store, e.pub, log)
	// Flushed on every path, including the early returns below. The last events
	// of a failed run are exactly the ones worth having.
	defer func() {
		closeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 15*time.Second)
		defer cancel()
		if err := state.buf.Close(closeCtx); err != nil {
			log.Error("could not flush trace buffer", "err", err)
		}
	}()

	// Per-run sockets. These are the container's only route out — it runs with
	// --network=none, so without them it can reach nothing at all.
	socketDir, err := proxy.RunDir(e.cfg.SocketBaseDir, run.ID)
	if err != nil {
		log.Error("could not create socket dir", "err", err)
		state.emitRunnerError(ctx, fmt.Sprintf("could not prepare sandbox: %v", err))
		return e.finish(ctx, run.ID, store.Completion{
			Status:     "failed",
			Error:      fmt.Sprintf("could not prepare sandbox: %v", err),
			DurationMS: msSince(started),
		})
	}
	defer func() {
		if err := proxy.CleanupRunDir(socketDir); err != nil {
			log.Warn("could not clean up socket dir", "dir", socketDir, "err", err)
		}
	}()

	// The caller's own provider key, collected from Redis and removed as it is
	// read. Runbox holds no key of its own; the configured ones below are a
	// fallback for self-hosted deployments that want to supply one.
	providerKey, err := e.providerKey(runCtx, run.ID)
	if err != nil {
		log.Error("no usable provider key", "err", err)
		state.emitRunnerError(ctx, err.Error())
		return e.finish(ctx, run.ID, store.Completion{
			Status:     "failed",
			Error:      err.Error(),
			DurationMS: msSince(started),
		})
	}

	// Both providers are mounted when a key is available for them. Which one the
	// agent talks to is decided by the model id, inside the sandbox — the proxy
	// only decides what is reachable at all.
	llmProxy, err := proxy.StartLLM(socketDir, proxy.LLMConfig{
		Upstreams: []proxy.Upstream{
			proxy.AnthropicUpstream(
				e.cfg.AnthropicBaseURL, providerKey.anthropic, e.cfg.AnthropicVersion,
			),
			proxy.OpenAIUpstream(e.cfg.OpenAIBaseURL, providerKey.openai),
		},
	}, log)
	if err != nil {
		log.Error("could not start llm proxy", "err", err)
		state.emitRunnerError(ctx, fmt.Sprintf("could not start llm proxy: %v", err))
		return e.finish(ctx, run.ID, store.Completion{
			Status:     "failed",
			Error:      fmt.Sprintf("could not start llm proxy: %v", err),
			DurationMS: msSince(started),
		})
	}
	defer llmProxy.Close()

	egressProxy, err := proxy.StartEgress(socketDir, e.cfg.EgressAllowlist, log)
	if err != nil {
		log.Error("could not start egress proxy", "err", err)
		state.emitRunnerError(ctx, fmt.Sprintf("could not start egress proxy: %v", err))
		return e.finish(ctx, run.ID, store.Completion{
			Status:     "failed",
			Error:      fmt.Sprintf("could not start egress proxy: %v", err),
			DurationMS: msSince(started),
		})
	}
	defer egressProxy.Close()

	container, err := e.sbx.Start(runCtx, sandbox.Spec{
		RunID:        run.ID,
		Task:         run.Task,
		Model:        run.Model,
		Tools:        run.Tools,
		SystemPrompt: run.SystemPrompt,
		Temperature:  run.Temperature,
		MaxTokens:    run.MaxTokens,
		Image:        e.cfg.AgentImage,
		MemoryMB:     e.cfg.MemoryLimitMB,
		CPUs:         e.cfg.CPULimit,
		PidsLimit:    e.cfg.PidsLimit,
		SocketDir:    socketDir,
		Env: map[string]string{
			// A placeholder, not a credential. The agent's HTTP client requires
			// the variable to be set; the real key is attached by the proxy,
			// upstream of anything the agent can observe.
			"ANTHROPIC_API_KEY":   "proxied-by-runner",
			"OPENAI_API_KEY":      "proxied-by-runner",
			"RUNBOX_LLM_SOCKET":   filepath.Join(proxy.SocketDir, proxy.LLMSocketName),
			"RUNBOX_PROXY_SOCKET": filepath.Join(proxy.SocketDir, proxy.EgressSocketName),
			"ANTHROPIC_BASE_URL":  "http://llm.runbox.internal",
			"OPENAI_BASE_URL":     "http://llm.runbox.internal",
		},
	})
	if err != nil {
		// A container that will not start is a failed run with a clear reason,
		// never a hang.
		log.Error("container failed to start", "err", err)
		state.emitRunnerError(ctx, fmt.Sprintf("container failed to start: %v", err))
		return e.finish(ctx, run.ID, store.Completion{
			Status:     "failed",
			Error:      fmt.Sprintf("container failed to start: %v", err),
			DurationMS: msSince(started),
		})
	}
	defer container.Remove()

	streamErr := container.Stream(runCtx,
		func(line []byte) error { return state.onLine(ctx, line) },
		func(line string) { log.Warn("agent stderr", "line", line) },
	)

	// The container may still be alive if streaming stopped early. Stop it
	// before reading the exit code so Wait cannot block on a live process.
	if runCtx.Err() != nil {
		if err := container.Kill(killGrace); err != nil {
			log.Warn("kill failed", "err", err)
		}
	}

	exitCode, waitErr := container.Wait(context.WithoutCancel(runCtx))

	// Flush before resolving. The terminal state is derived from the final
	// event, and the usage row is written from it — both would race the
	// background ticker otherwise.
	flushCtx, flushCancel := context.WithTimeout(context.WithoutCancel(ctx), 15*time.Second)
	if err := state.buf.Close(flushCtx); err != nil {
		log.Error("could not flush trace buffer", "err", err)
	}
	flushCancel()

	completion := state.resolve(runCtx, streamErr, waitErr, exitCode, started)

	// Usage is recorded for every terminal state, including timeout and cancel.
	// A run that burned 40,000 tokens before being cancelled consumed 40,000
	// tokens, and a metering system that quietly forgets that is not one.
	cost := e.recordUsage(ctx, run, state, completion.DurationMS, log)

	log.Info("run finished",
		"status", completion.Status, "duration_ms", completion.DurationMS,
		"events", state.seq, "exit", exitCode, "cost_micros", cost)

	return e.finish(ctx, run.ID, completion)
}

func (e *Executor) recordUsage(
	ctx context.Context, run *store.Run, state *runState, durationMS int, log *slog.Logger,
) int64 {
	// Prefer the final event, fall back to the last per-turn usage report, and
	// fall back again to what the runner observed directly. The chain is what
	// makes a timed-out or cancelled run billable rather than free.
	reported := state.lastUsage
	if state.final != nil {
		reported = state.final.Usage
	}

	usage := store.Usage{
		InputTokens:  reported.InputTokens,
		OutputTokens: reported.OutputTokens,
		ToolCalls:    reported.ToolCalls,
		ComputeMS:    durationMS,
	}
	// The runner's own count is a floor: it saw every tool_call event that was
	// written, even ones the agent never got to report.
	if state.toolCalls > usage.ToolCalls {
		usage.ToolCalls = state.toolCalls
	}

	writeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
	defer cancel()

	cost, err := e.store.RecordUsage(writeCtx, run.ID, run.TenantID, run.Model, usage)
	if err != nil {
		// Not fatal to the run, but it is a billing gap, so it is logged loudly.
		log.Error("could not record usage", "err", err)
		return 0
	}
	return cost
}

func (e *Executor) finish(ctx context.Context, runID string, c store.Completion) error {
	// Deliberately not tied to runCtx: a timed-out run still has to record that
	// it timed out.
	writeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
	defer cancel()
	return e.store.Finish(writeCtx, runID, c)
}

// runState carries the per-run mutable bits: the sequence counter and whatever
// the agent's final event told us.
type runState struct {
	run  *store.Run
	exec *Executor
	log  *slog.Logger

	seq   int
	final *trace.Final

	// lastUsage is the most recent cumulative report from the agent. It is what
	// makes a killed run billable: the agent reports after every turn, so even
	// a container that never reached its final event leaves an accurate figure
	// behind.
	lastUsage trace.Usage
	toolCalls int

	// lastError is the most recent error the agent reported.
	//
	// The agent's final event carries a status but no reason, so without this
	// a run that died on a provider 401 lands in the runs list as "failed" with
	// an empty error column — the explanation visible only to someone who
	// thinks to open the trace. The reason is right there in the stream; it
	// just has to be carried across.
	lastError string

	// buf batches trace writes. Nil only in tests that do not exercise
	// persistence.
	buf *traceBuffer
}

func (s *runState) onLine(ctx context.Context, line []byte) error {
	s.seq++
	event, err := trace.Parse(line, s.seq)
	if err != nil {
		// Malformed line: record it and keep going. One bad line must not cost
		// the run.
		s.log.Warn("malformed agent output", "seq", s.seq, "err", err)
		event = trace.ErrorEvent(s.seq, time.Now().UnixMilli(),
			fmt.Sprintf("malformed agent output: %v", err))
	}

	switch event.Type {
	case trace.TypeToolCall:
		s.toolCalls++

	case trace.TypeUsage:
		if usage, err := event.DecodeUsage(); err == nil {
			s.lastUsage = usage
		} else {
			s.log.Warn("undecodable usage event", "seq", event.Seq, "err", err)
		}

	case trace.TypeError:
		if payload, err := event.DecodeError(); err == nil && payload.Message != "" {
			s.lastError = payload.Message
		}

	case trace.TypeFinal:
		if final, err := event.DecodeFinal(); err == nil {
			s.final = &final
		} else {
			s.log.Warn("undecodable final event", "err", err)
		}
	}

	return s.persist(ctx, event)
}

func (s *runState) persist(ctx context.Context, event trace.Event) error {
	// Writes use the parent context, not the run's: an event produced in the
	// last moments before a timeout still belongs in the trace.
	writeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
	defer cancel()

	// Buffered rather than written straight through. Losing the durable copy is
	// still worth failing the run for — the trace is the product — so Add
	// surfaces the first write error it saw.
	if err := s.buf.Add(writeCtx, event); err != nil {
		return fmt.Errorf("persist event %d: %w", event.Seq, err)
	}
	return nil
}

func (s *runState) emitRunnerError(ctx context.Context, message string) {
	s.seq++
	event := trace.ErrorEvent(s.seq, time.Now().UnixMilli(), message)
	if err := s.persist(ctx, event); err != nil {
		s.log.Error("could not record runner error", "err", err)
	}
}

// resolve decides the terminal status from everything we observed.
func (s *runState) resolve(
	runCtx context.Context, streamErr, waitErr error, exitCode int64, started time.Time,
) store.Completion {
	c := store.Completion{DurationMS: msSince(started)}

	switch {
	case errors.Is(runCtx.Err(), context.DeadlineExceeded):
		c.Status = "timeout"
		c.Error = "run exceeded its timeout"

	case errors.Is(runCtx.Err(), context.Canceled):
		c.Status = "cancelled"
		c.Error = "run was cancelled"

	case s.final != nil:
		c.Status = s.final.Status
		c.Result = s.final.Result
		if c.Status == "" {
			c.Status = "succeeded"
		}
		// The final event says *that* it failed; the error event before it says
		// why. Only the pair together is any use to someone reading the runs
		// list, so a terminal status other than success takes the reason with
		// it.
		if c.Status != "succeeded" {
			c.Error = s.lastError
			if c.Error == "" {
				c.Error = "agent reported " + c.Status + " without an error"
			}
		}

	case streamErr != nil:
		c.Status = "failed"
		c.Error = fmt.Sprintf("stream error: %v", streamErr)

	case waitErr != nil:
		c.Status = "failed"
		c.Error = fmt.Sprintf("wait error: %v", waitErr)

	case exitCode != 0:
		c.Status = "failed"
		c.Error = fmt.Sprintf("agent exited with code %d", exitCode)

	default:
		// Clean exit with no final event. Rare, and worth naming precisely
		// rather than reporting as success.
		c.Status = "failed"
		c.Error = "agent exited without emitting a final event"
	}
	return c
}

func msSince(t time.Time) int {
	return int(time.Since(t).Milliseconds())
}

// runKeys is which provider credentials this run may use.
//
// Both fields are usually empty except the one matching the run's model: a
// caller supplies the key for the provider they are using, and mounting an
// upstream with no key would give the agent a 401 from somebody else's API
// instead of a clear refusal from ours.
type runKeys struct {
	anthropic string
	openai    string
}

// providerKey resolves the credential for a run.
//
// The caller's own key wins. A key configured on the runner is a fallback for
// self-hosted deployments that prefer to supply one centrally; the hosted
// deployment sets neither, so a run without a caller key fails here rather than
// silently spending somebody else's budget.
func (e *Executor) providerKey(ctx context.Context, runID string) (runKeys, error) {
	var supplied string
	if e.keys != nil {
		var err error
		supplied, err = e.keys.TakeProviderKey(ctx, runID)
		if err != nil {
			// A Redis failure here is not the caller's fault, and retrying the
			// run would be reasonable — but the key is gone either way, so
			// failing with a clear reason beats hanging.
			return runKeys{}, fmt.Errorf("could not read the provider key for this run: %w", err)
		}
	}

	keys := runKeys{anthropic: e.cfg.AnthropicAPIKey, openai: e.cfg.OpenAIAPIKey}

	if supplied != "" {
		// Routed by prefix, matching the agent's own provider selection. The
		// control plane already rejected a key that does not match the model,
		// so this only has to place it.
		switch {
		case strings.HasPrefix(supplied, "sk-ant-"):
			keys.anthropic = supplied
		case strings.HasPrefix(supplied, "sk-"):
			keys.openai = supplied
		default:
			return runKeys{}, fmt.Errorf("supplied provider key is not a recognised format")
		}
	}

	if keys.anthropic == "" && keys.openai == "" {
		return runKeys{}, fmt.Errorf(
			"no provider key for this run: supply one in the X-Provider-Key header")
	}
	return keys, nil
}
