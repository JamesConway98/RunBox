# Runbox

**A sandboxed execution and observability platform for LLM agents.**

Submit a task over an API. The agent runs inside an isolated container with
tool-calling. The execution trace streams to a dashboard in real time. Every run
is metered per tenant.

[**Live demo**](https://runbox.jamesconwaydev.com/demo) · no signup ·
[API docs](https://api.runbox.jamesconwaydev.com/docs) ·
[Python SDK](sdk/python)

---

## Architecture

```
                    ┌──────────────────────────┐
                    │   Dashboard (Next.js)    │
                    │   React + TypeScript     │
                    └───────────┬──────────────┘
                                │ REST + SSE
                                ▼
                    ┌──────────────────────────┐
                    │  Control plane (Python)  │
                    │  FastAPI                 │
                    │  • auth / tenancy        │
                    │  • run CRUD              │
                    │  • SSE fan-out           │
                    │  • usage rollups         │
                    └─────┬──────────────┬─────┘
                          │              │
                   enqueue│              │read/write
                          ▼              ▼
                ┌──────────────┐   ┌──────────────┐
                │    Redis     │   │  PostgreSQL  │
                │ queue + pub  │   │  system of   │
                │    /sub      │   │   record     │
                └──────┬───────┘   └──────▲───────┘
                       │                  │
                       ▼                  │
            ┌────────────────────────┐    │
            │   Runner (Go)          │────┘
            │   • worker pool        │
            │   • container lifecycle│
            │   • trace publishing   │
            │   • cancellation       │
            └──────────┬─────────────┘
                       │ docker API
                       ▼
            ┌────────────────────────┐
            │  Sandbox container     │
            │  agent.py (Python)     │
            │  • LLM loop            │
            │  • tool-calling        │
            │  • emits JSONL trace   │
            └────────────────────────┘
```

### Why three languages

Each does what it is genuinely best at, and I would rather defend that than
pretend one runtime was obviously right.

**Go for the runner.** Its entire job is container lifecycle, a bounded worker
pool, per-run timeouts, and cancellation that actually propagates to a child
process. That is what `context.Context`, goroutines and channels exist for. In
Python it would be a fight with the GIL and async plumbing for no benefit.

**Python for the control plane and the agent.** FastAPI for the API surface, and
the agent lives where the LLM SDKs and tooling ecosystem are.

**TypeScript for the dashboard.** Live-updating UI over SSE.

The obvious objection is that a single Go service could do all of it, and that
is true. The answer is that the agent loop belongs in Python, and once it is a
separate process anyway, splitting the runner out is what makes cancellation and
isolation clean rather than tangled through one binary.

---

## The five decisions worth reading the code for

### 1. SSE, and the join that makes it gapless

`GET /v1/runs/{id}/stream` returns `text/event-stream`. Not WebSockets — the
data flows one way, so a duplex protocol would be paying connection complexity
for a direction never used. SSE also reconnects natively via `Last-Event-ID` and
survives proxies that still mangle WebSocket upgrades.

The transport is the easy part. The hard part is joining what is already durable
in Postgres to what is about to arrive on Redis
([`sse.py`](control-plane/runbox_api/sse.py)):

1. Subscribe to Redis **first**, pumping arrivals into a buffer.
2. Replay from Postgres.
3. Drain the buffer, discarding anything the replay already covered.
4. Go live.

Subscribing after the replay drops every event produced while the query ran.
Replaying after going live delivers them out of order. Deduplication is by
`seq`, which is unique per run and monotonic, so "already covered" is a
comparison rather than a guess.

### 2. Metering that survives failure

Cost is computed at write time from the pricing row in force and stored. Never
recomputed on read — prices change, historical costs must not.

`cost_micros` is a `bigint`. Money in floats is a bug waiting to happen.
Arithmetic is integer throughout with division last and rounding **up**, because
rounding down means systematically undercharging a fraction of a micro on every
single run, which is the kind of thing found during an audit rather than during
development.

Usage is recorded for **every** terminal state, including timeout and cancel.
The agent reports cumulative usage after each model turn rather than only at the
end, which is what makes a killed run billable. A metering system that loses
everything when a run is interrupted undercounts precisely when something went
wrong.

### 3. The sandbox, described honestly

Every run gets a container with `--network=none`, a read-only root filesystem, a
`noexec` tmpfs at `/tmp`, `--cap-drop=ALL`, `no-new-privileges`, a non-root
user, and memory, CPU and pids limits. The wall-clock timeout is enforced by the
runner, not by the container.

`--network=none` is a real setting rather than one that gets quietly reverted
the first time the agent needs an API. The container reaches the outside world
through two unix sockets the runner controls
([`internal/proxy`](runner/internal/proxy)):

- **`llm.sock`** — a reverse proxy to the model provider. The runner holds the
  API key and attaches it upstream, so the most valuable credential in the
  system is never inside the least trusted process in it. The agent's
  environment carries a placeholder. Agent-supplied auth headers are stripped
  before forwarding, and only the one endpoint the agent needs is routed.
- **`egress.sock`** — the `http_get` tool, with a host allowlist. Every redirect
  hop is re-checked, because requesting an allowed host that 302s elsewhere is
  the obvious way around an allowlist.

**What this is not:** a hostile-tenant boundary. It is a hardened sandbox.
A genuinely hostile multi-tenant guarantee wants microVMs (Firecracker) or
gVisor, which is the natural next step and is deliberately out of scope. Knowing
where the boundary of the claim sits is more useful than overstating it.

### 4. Tenant isolation in the database, not just the application

Every tenant-scoped table carries `tenant_id` and has a row-level security
policy keyed on a session GUC that the control plane sets per request, scoped to
the transaction so it cannot outlive a pooled connection.

The application already filters by `tenant_id` everywhere. RLS exists because
"we remembered the where clause every time" is a promise about human attention,
and one missing predicate in one endpoint is a cross-tenant leak. Two roles:
`runbox_api`, which does not own the tables so policies actually apply to it,
and `runbox_runner`, which bypasses RLS because it legitimately executes work
for every tenant.

CI queries `pg_tables` to assert RLS is on. A new table added without a policy
is exactly the mistake nobody notices.

### 5. Client state that is actually hard

The Playground runs N panes against N models from one prompt, each with its own
SSE stream appending independently. Nothing coordinates between them, which is
what makes it a real test rather than a demo.

Three non-obvious decisions in
[`useRunStream.ts`](web/src/lib/useRunStream.ts):

- **Tokens collapse into text segments, not timeline rows.** A long run emits
  thousands of `token` events; one DOM node each means React diffing 8,000
  siblings every frame.
- **Updates batch to an animation frame.** A fast stream produces tokens faster
  than the display refreshes; one `setState` per token is dozens of wasted
  renders per frame.
- **One `AbortController` per pane.** A shared one means cancelling a single
  pane takes down every pane's in-flight request — precisely the bug the screen
  exists to prove is absent.

The batch results table virtualises thousands of rows with no pagination
([`useVirtualRows.ts`](web/src/lib/useVirtualRows.ts)), hand-rolled in sixty
lines because the requirement is uniform row heights and one scroller. Because
every row is already client-side, filtering and sorting are synchronous array
operations rather than a round trip and a spinner.

---

## What this deliberately does not do

Scoping is a design decision, so it is worth stating rather than leaving as a
gap someone has to discover.

- **Not a general-purpose agent framework.** No plugin system, no DSL. One agent
  loop, done properly.
- **Not real billing.** Metering produces cost rows. No payment capture.
- **Not a production security boundary.** See above.
- **No horizontal autoscaling.** One runner process with a bounded worker pool.
  Designed for, not built.
- **No user-generated tool code.** The registry is fixed in the repo.

---

## What breaks first

Asked in every interview, so here is the honest list:

| Limit | Why | Next step |
|---|---|---|
| Single runner process | Bounded pool on one host | Several runners; `SKIP LOCKED` already makes it safe |
| Redis list as a queue | No dead-letter, no visibility timeout | Streams with consumer groups, or a real broker |
| Usage rollups on read | `date_trunc` group-by per request | Rollup table written on completion |
| Orphan detection by deadline | Not a lease | Heartbeat leases once there is more than one runner |
| Trace events unbounded | Every event kept forever | Partition by month, archive to object storage |

The Redis queue is acceptable today only because it is not the system of record.
The `runs` table is, and the runner falls back to claiming from Postgres — which
is what makes a lost queue entry recoverable rather than a lost run.

---

## Running it

```bash
make up             # postgres + redis
make db-migrate
make db-seed        # prints API keys once
make agent-image
```

Then in separate terminals:

```bash
make api            # :8000
make runner         # needs a docker socket
make web            # :3000
```

```bash
curl -X POST localhost:8000/v1/runs \
  -H "Authorization: Bearer rb_live_..." \
  -H "Content-Type: application/json" \
  -d '{"task":"What is 17 * 23?","model":"claude-haiku-4-5"}'
```

`make test` runs every suite. `make lint` lints every service.

---

## Layout

| Path | What |
|---|---|
| [`db/`](db) | Migrations. Forward-only SQL, no framework |
| [`control-plane/`](control-plane) | FastAPI: auth, run CRUD, SSE, usage, batches, evals |
| [`runner/`](runner) | Go: worker pool, container lifecycle, proxies, reaper |
| [`agent/`](agent) | The LLM loop that runs inside the sandbox |
| [`sdk/python/`](sdk/python) | Python client with resumable streaming |
| [`web/`](web) | Next.js dashboard |

## Evidence

The code is not really the deliverable. These are:

- [x] **Public repo**, opening with the architecture and the decisions
- [x] **Live demo**, no signup, seeded runs and a working "run this" button
- [x] **SDK** — `pip install runbox`, with three runnable examples
- [x] **CI** across all five services, including assertions a test suite cannot
      make (RLS enabled everywhere, migrations idempotent, agent image non-root)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and the conventions that are
load-bearing rather than stylistic.

## License

MIT
