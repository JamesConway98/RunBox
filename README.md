# Runbox

A sandboxed execution and observability platform for LLM agents.

Submit a task over an API, the agent runs inside an isolated container with
tool-calling, the execution trace streams to a dashboard in real time, and every
run is metered per tenant.

> Status: early. Scaffold only.

## Layout

```
db/             SQL migrations (Postgres, RLS-enforced multi-tenancy)
control-plane/  FastAPI: auth, run CRUD, SSE fan-out, usage rollups
runner/         Go: worker pool, container lifecycle, trace publishing
agent/          Python: the LLM loop that runs inside the sandbox
sdk/python/     Python client
web/            Next.js dashboard
```

## License

MIT
