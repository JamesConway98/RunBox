# runbox

Python client for [Runbox](https://github.com/JamesConway98/RunBox) — sandboxed
execution and observability for LLM agents.

```bash
pip install -e sdk/python     # not on PyPI yet
```

## Quick start

```python
from runbox import Runbox

rb = Runbox(api_key="rb_live_...")  # or set RUNBOX_API_KEY

run = rb.runs.create(
    task="Find the three most recent Go releases and summarise them",
    tools=["http_get"],
)

for event in run.stream():
    if event.type == "tool_call":
        print(f"\n→ {event.tool}({event.args})")
    elif event.type == "token":
        print(event.text, end="", flush=True)

print(f"\n\n{run.usage.total_tokens} tokens, ${run.usage.cost:.4f}")
```

## Three things worth knowing

**Streaming reconnects on its own.** A dropped connection resumes from the last
`seq` seen; the server replays from Postgres and switches to live, so the
generator you are iterating simply keeps yielding. No gap, no duplicates.

**Cost is integer micros.** `usage.cost_micros` is the authoritative value.
`usage.cost` is a float for printing — never sum those. Add the micros and
convert once at the end.

**Only idempotent requests are retried.** A retried `POST /v1/runs` that already
succeeded would create a second run and bill for it, so `create` is never
retried automatically.

## Waiting instead of streaming

If you only want the answer, do not pay for the trace:

```python
run = rb.runs.create(task="What is 17 * 23?").wait(timeout=60)
print(run.result)
```

`wait()` polls rather than holding a connection open, which makes it usable
behind proxies and in short-lived environments where a long-lived stream is
awkward.

## Listing runs

```python
# One page
for run in rb.runs.list(status="failed", limit=10):
    print(run.id, run.error)

# Every run, following cursors — a generator, so you can stop early
for run in rb.runs.iterate(model="claude-sonnet-5"):
    if run.usage.cost_micros > 10_000:
        print(f"{run.id} cost ${run.usage.cost:.4f}")
        break
```

## Errors

```python
from runbox import RateLimitError, InvalidRequestError, RunboxError

try:
    run = rb.runs.create(task="…", model="not-a-model")
except InvalidRequestError as exc:
    print(exc.message)      # retrying will not help
except RateLimitError as exc:
    print(exc.retry_after)  # it will, after this long
except RunboxError:
    ...                     # catches everything this library raises
```

## Configuration

| Argument | Environment | Default |
|---|---|---|
| `api_key` | `RUNBOX_API_KEY` | — (required) |
| `base_url` | `RUNBOX_BASE_URL` | `http://localhost:8000` |
| `timeout` | — | `30.0` |
| `max_retries` | — | `3` |

The client is a context manager, which is the tidy way to close the underlying
connection pool:

```python
with Runbox() as rb:
    run = rb.runs.create(task="…").wait()
```

## Examples

Three runnable scripts in [`examples/`](examples/):

- `basic.py` — create a run and print the result
- `streaming.py` — render a live trace in the terminal
- `compare_models.py` — the same prompt across several models, concurrently

## License

MIT
