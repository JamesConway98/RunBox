# agent

The LLM loop that runs inside the sandbox. One container per run, created and
destroyed by the Go runner.

## Contract

The agent is configured entirely through the environment and communicates
entirely through stdout.

| Variable | Meaning |
|---|---|
| `RUNBOX_TASK` | The task text. Required. |
| `RUNBOX_MODEL` | Model id, e.g. `claude-sonnet-5`. |
| `RUNBOX_TOOLS` | Comma-separated tool names from the registry. |
| `RUNBOX_SYSTEM_PROMPT` | Optional override of the default system prompt. |
| `RUNBOX_MAX_TOKENS` | Token ceiling for the whole run. |
| `RUNBOX_TEMPERATURE` | Optional float. |
| `RUNBOX_PROXY_SOCKET` | Unix socket for the runner's egress proxy. |
| `ANTHROPIC_API_KEY` | Injected by the runner, never by the caller. |

Output is newline-delimited JSON on stdout, one object per event:

```json
{"type":"llm_call","ts":1722600000000,"model":"claude-sonnet-5","messages":1,"tools":["http_get"]}
{"type":"token","ts":1722600000420,"text":"Based on"}
{"type":"tool_call","ts":1722600001100,"tool":"http_get","args":{"url":"..."},"call_id":"toolu_01"}
{"type":"tool_result","ts":1722600001900,"tool":"http_get","call_id":"toolu_01","ok":true,"output":"HTTP 200\n\n...","duration_ms":780}
{"type":"final","ts":1722600004000,"status":"succeeded","result":"...","usage":{"input_tokens":8123,"output_tokens":412,"tool_calls":1}}
```

The agent always exits through a `final` event, including on failure. A run
that produces no `final` is one the runner killed, and the runner is
responsible for recording that.

## Tools

Fixed registry in `tools.py`. There is no user-supplied tool code — that is a
stated non-goal.

- `http_get` — proxied through the runner with a host allowlist
- `read_file` — read-only, confined to `/workspace`
- `list_files` — same confinement

## Local run

```bash
pip install -r requirements.txt
RUNBOX_TASK="What is 2+2?" RUNBOX_MODEL=claude-sonnet-5 python agent.py
```

Without the proxy socket present, `http_get` fails cleanly rather than falling
back to real network access.
