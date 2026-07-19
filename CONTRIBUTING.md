# Working on Runbox

## Setup

```bash
make setup      # every service's dependencies
make up         # postgres + redis
make db-migrate
make db-seed    # prints API keys once — they are only shown here
```

## Before pushing

```bash
make lint
make test
```

CI runs the same things plus a few it can and a laptop cannot: applying every
migration twice against a real Postgres, asserting RLS is enabled on every
tenant-scoped table, and checking the agent image runs as a non-root user.

## Conventions

**Migrations are forward-only.** Add a new numbered file; never edit one that
has been applied. There is no down migration, deliberately — a rollback that
has never been rehearsed is a rollback that will not work when it matters.

**Trace events are append-only.** Nothing updates a row after insert. That is
what makes them safe to stream and replay, and a great deal of the design rests
on it.

**Money is integer micros.** `bigint`, never a float, and never a `numeric` that
rounds at read time. Divide last and round up.

**Secrets never reach the sandbox.** The agent gets a placeholder API key; the
runner's proxy attaches the real one upstream. If you find yourself adding a
credential to `sandbox.Spec.Env`, that is the wrong layer.

**New tables need a policy.** Every tenant-scoped table gets `tenant_id`, RLS
enabled, `force row level security`, and a policy keyed on
`runbox_current_tenant()`. CI fails if you forget, which is the point.

## Adding a tool

Tools live in [`agent/tools.py`](agent/tools.py) and nowhere else — there is no
plugin system and that is a stated non-goal. A new tool is an entry in
`REGISTRY` plus a function. If it needs the network, it goes through the egress
proxy; the container has no route out on its own.

## Adding a model

1. A row in `model_pricing` via a migration.
2. An entry in [`web/src/lib/models.ts`](web/src/lib/models.ts).
3. If it is a new provider: a class implementing the `Provider` protocol in
   `agent/`, a prefix in `agent/providers.py`, and an `Upstream` in the runner's
   LLM proxy.

The third step was two files and some rows when OpenAI was added, which is the
return on having built the seam early rather than the day it was needed.

## Testing philosophy

Unit tests cover the things that are cheap to get wrong and expensive to notice:
cost arithmetic, SSE framing, the egress allowlist, dataset parsing, scorer
determinism. There is no end-to-end suite — it would need Docker, a database,
Redis and a provider key, and the honest version of that is running the thing
locally.

Where a test asserts something non-obvious, the comment says *why*, so that
someone changing the behaviour later knows whether they are fixing a bug or
breaking a guarantee.
