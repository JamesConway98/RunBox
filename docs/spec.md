# Runbox — design notes

The full technical spec lives outside the repo; this file records the decisions
that survived contact with the implementation, plus the ones that changed.

## Shape

Three services, three languages, each doing what it is genuinely best at.

- **Go for the runner.** Container lifecycle, a bounded worker pool, per-run
  timeouts and cancellation propagation are exactly what goroutines, channels
  and `context.Context` exist for.
- **Python for the control plane and the agent.** FastAPI for the API surface;
  the agent lives where the LLM SDKs are.
- **TypeScript / Next.js for the dashboard.** Live-updating UI over SSE.

A single Go service could do all of this. The reason it does not: the agent loop
belongs in Python, and splitting the runner out is what makes cancellation and
isolation clean.

## Non-goals

- Not a general-purpose agent framework. One agent loop, done well.
- Not real billing. Metering produces cost rows. No payment capture.
- Not a production security boundary. Hardened containers, honestly documented.
- No horizontal autoscaling. One runner process, bounded worker pool.
- No user-generated tool code. Tools are a fixed registry in the repo.

## Open questions

- Redis list as a queue has no dead-letter handling. Acceptable at this scale;
  note it as the first thing that breaks.
- Usage rollups are computed on read. A materialised view is the next step.
