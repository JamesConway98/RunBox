# Deployment

Runbox is not one deployable. The dashboard is a static-ish Next.js app; the
control plane holds long-lived SSE connections; the runner needs a Docker
socket. Those are three different hosting shapes and pretending otherwise means
picking the wrong platform for two of them.

| Piece | Where | Why |
|---|---|---|
| `web/` | **Vercel** → `runbox.jamesconwaydev.com` | Plain Next.js. |
| `control-plane/` | **Fly** → `api.runbox.jamesconwaydev.com` | A run may take 600s; a stream outlives most function timeouts. |
| `runner/` | **Fly machine** | Needs a Docker socket. This rules out every platform that does not give you a host. |
| Postgres | **Neon** | Managed. Do not run your own. |
| Redis | **Upstash** | Same. |

The runner and the API sit on the same Fly org because the runner needs a
machine anyway, and running the API next to it is one less thing to operate.

## DNS

`jamesconwaydev.com` is the hub; each project gets a subdomain.

```
runbox      CNAME  cname.vercel-dns.com.
api.runbox  CNAME  runbox-api.fly.dev.
```

The apex and `www` stay pointed at the personal site. Nothing here touches
them, which is the point of putting each project on its own subdomain rather
than a path — a broken deploy of one project cannot take down the hub.

## Order

The runner cannot start without a database, and the dashboard is useless
without an API, so:

```bash
# 1. Data
neon projects create --name runbox
upstash redis create --name runbox

# 2. Schema
DATABASE_URL=... ./scripts/migrate.sh
DATABASE_URL=... python scripts/seed.py     # prints keys once

# 3. Agent image, pinned by digest
docker build -t ghcr.io/jamesconway98/runbox-agent:$(git rev-parse --short HEAD) ./agent
docker push ghcr.io/jamesconway98/runbox-agent:$(git rev-parse --short HEAD)
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/jamesconway98/runbox-agent:...
# put that digest in deploy/fly.runner.toml

# 4. Services
fly deploy -c deploy/fly.api.toml
fly deploy -c deploy/fly.runner.toml
vercel --cwd web --prod
```

## Secrets

Never in a config file, and never in the dashboard's environment — the browser
receives everything a Next.js app is given that is prefixed `NEXT_PUBLIC_`, and
the provider keys are not the dashboard's business in the first place.

```bash
fly secrets set -a runbox-api \
  DATABASE_URL=... REDIS_URL=...

fly secrets set -a runbox-runner \
  DATABASE_URL=... REDIS_URL=... \
  ANTHROPIC_API_KEY=... OPENAI_API_KEY=...
```

Note which app holds which. The API never sees a provider key: it does not call
a model. Only the runner does, and it hands the sandbox a placeholder while the
proxy attaches the real one upstream.

## The Docker socket

The runner's machine mounts its own Docker socket. That is the one genuinely
privileged thing in this deployment, and it is worth stating plainly: anything
that can reach that socket is root on the host.

The mitigation is that the machine runs nothing else, has no public HTTP
service, and takes work only off a queue. It is not a substitute for the microVM
isolation named as out of scope in the README — it is the reason that boundary
matters.
