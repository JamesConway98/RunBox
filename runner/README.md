# runner

The execution engine. Claims queued runs, creates one sandbox container per
run, streams the agent's stdout into `trace_events`, and records the terminal
state.

Go, because this component's entire job is concurrency and process lifecycle —
a bounded worker pool, per-run timeouts, and cancellation that actually
propagates to a child process. That is what `context.Context`, goroutines and
channels are for. In Python it would be a fight with the GIL for no benefit.

## Layout

```
cmd/runner/          entrypoint, signal handling, poll loop
internal/config/     environment-driven configuration
internal/store/      Postgres: claim work, append events, finish runs
internal/sandbox/    Docker: create, stream, kill, remove
internal/trace/      the event vocabulary shared with the agent
internal/worker/     executor — one run, start to finish
```

## Design notes

**Claiming is `FOR UPDATE SKIP LOCKED`.** Two runner processes can coexist
safely even though the deployment only runs one. A worker that finds the head
row locked moves past it instead of queueing behind it.

**Sequence numbers are assigned here.** The agent emits unordered events; the
runner is the only component that sees a run's whole stream, so it is the only
one that can guarantee monotonicity. `(run_id, seq)` is unique in Postgres and
inserts are `on conflict do nothing`, so a retried write cannot put a duplicate
into a stream that clients resume from by cursor.

**A failed run is not a failed job.** `Execute` returns an error only when the
runner itself broke. A run whose agent crashed is a job that completed with
status `failed`, and is recorded as such.

**Trace writes use the parent context, not the run's.** An event produced in
the last moments before a timeout still belongs in the trace, and a timed-out
run still has to record that it timed out.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | — | Required. |
| `ANTHROPIC_API_KEY` | — | Required. Injected into the sandbox by the runner. |
| `RUNBOX_AGENT_IMAGE` | `runbox/agent:dev` | Pin by digest in production. |
| `RUNBOX_WORKERS` | `GOMAXPROCS` | Worker pool size. |
| `RUNBOX_DEFAULT_TIMEOUT` | `120s` | Used when a run omits `timeout_s`. |
| `RUNBOX_MAX_TIMEOUT` | `600s` | Hard ceiling; a caller cannot exceed it. |
| `RUNBOX_MEMORY_MB` | `512` | Per-container memory limit. |
| `RUNBOX_CPUS` | `1.0` | Per-container CPU limit. |
| `RUNBOX_PIDS_LIMIT` | `128` | Contains fork bombs. |

## Running

```bash
go build -o bin/runner ./cmd/runner
DATABASE_URL=postgres://... ANTHROPIC_API_KEY=sk-... ./bin/runner
```

Requires a reachable Docker socket. The runner pings it at boot so that a
missing socket is a startup failure rather than a mystery on the first run.
