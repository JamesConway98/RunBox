# Deployment

Runbox is not one deployable, and the reason is one component with an unusual
requirement.

| Piece | Where | Why |
|---|---|---|
| `web/` | **Vercel** → `runbox.jamesconwaydev.com` | Plain Next.js. |
| `control-plane/` | **Railway** → `api.runbox.jamesconwaydev.com` | Long-lived SSE connections; a run may take 600s. |
| Postgres | **Railway** | Same project, `DATABASE_URL` wired automatically. |
| Redis | **Railway** | Same. |
| `runner/` | **A small VM with Docker** | Needs a real Docker socket. See below. |

## Why the runner cannot go on a PaaS

The runner creates a container per run. That needs a reachable
`/var/run/docker.sock`, and **no build-and-run platform provides one** — not
Railway, not Fly, not Render. This is not a Railway limitation to work around;
it is the defining property of the component.

An earlier version of this file claimed a Fly machine "mounts its own Docker
socket". That was wrong. Fly Machines are Firecracker microVMs running your
image; they no more hand you a Docker daemon than Railway does. Correcting it
is worth more than quietly deleting it, because it is an easy mistake to repeat.

What actually works is a plain VM where you install Docker and run the binary on
the host. Hetzner CX22 (~€4/mo) or a DigitalOcean droplet ($6/mo) is ample — the
runner is a few MB of Go, and the containers it creates are capped at 512MB
each.

The honest alternative, if a VM to patch is not appealing: rewrite
`internal/sandbox` to drive the Fly Machines API and get a microVM per run
instead of a container. That is *stronger* isolation — it is the Firecracker
step the main README names as out of scope — but it is a rewrite of that
package, not a config change.

---

## 1. Railway: databases and the API

```bash
railway login
railway init --name runbox

railway add --database postgres
railway add --database redis

# The API builds from control-plane/Dockerfile via control-plane/railway.json.
railway up --service api --path-as-root control-plane
```

Railway injects `DATABASE_URL` and `REDIS_URL` into services in the same
project, so the control plane needs little else:

```bash
railway variables --service api \
  --set "CORS_ORIGINS=https://runbox.jamesconwaydev.com" \
  --set "DEMO_RATE_LIMIT_PER_HOUR=5" \
  --set "LOG_LEVEL=info"
```

Note what is **not** set there: no provider key. The control plane never calls a
model, so it has no business holding one.

Then a public domain:

```bash
railway domain --service api api.runbox.jamesconwaydev.com
```

## 2. Schema

Run from your laptop against Railway's **public** connection string — the
`.railway.internal` hostnames only resolve inside their network.

```bash
export DATABASE_URL="postgresql://postgres:...@<host>.proxy.rlwy.net:PORT/railway"
./scripts/migrate.sh
control-plane/.venv/bin/python scripts/seed.py   # prints API keys once
```

## 3. Agent image, pinned by digest

```bash
TAG=$(git rev-parse --short HEAD)
docker build -t ghcr.io/jamesconway98/runbox-agent:$TAG ./agent
docker push ghcr.io/jamesconway98/runbox-agent:$TAG
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/jamesconway98/runbox-agent:$TAG
```

Put that `@sha256:...` in the VM's `runner.env`. A tag cannot answer "which code
actually ran", which is the first question asked about any agent trace.

## 4. The runner VM

```bash
scp -r deploy/runner-vm root@your-box:/tmp/
ssh root@your-box 'bash /tmp/runner-vm/setup.sh'
ssh root@your-box 'vi /etc/runbox/runner.env'    # fill in the required values

./deploy/runner-vm/deploy.sh root@your-box
```

`setup.sh` installs Docker, creates an unprivileged `runbox` account and
installs the systemd unit. `deploy.sh` cross-compiles for linux/amd64 and ships
the binary, so the VM never needs a Go toolchain.

The runner runs **natively, not in a container**, and that is deliberate. It
creates unix sockets and bind-mounts them into each agent container; if the
runner were itself containerised, the path it writes to and the path the Docker
daemon resolves for the mount would be different namespaces, and every run would
fail with "egress proxy unavailable". On the host they are the same path, and
the whole class of bug disappears.

## 5. Dashboard

```bash
vercel --cwd web --prod
vercel alias set <deployment-url> runbox.jamesconwaydev.com
```

Set `RUNBOX_API_URL=https://api.runbox.jamesconwaydev.com` in the Vercel project
so the rewrite in `next.config.ts` proxies to the real API.

Check the alias actually moved. `vercel --prod` reports success while leaving a
domain pointed at an older deployment, which is a genuinely confusing failure
mode and has already happened once on this account:

```bash
vercel alias ls | grep runbox
```

## DNS

`jamesconwaydev.com` is the hub; each project gets a subdomain.

```
runbox      CNAME  cname.vercel-dns.com.
api.runbox  CNAME  <project>.up.railway.app.
```

The apex and `www` stay on the personal site. Nothing here touches them — which
is the point of a subdomain per project rather than a path: a broken deploy of
one cannot take down the hub.

## Where the secrets live

| Secret | Railway (API) | Runner VM |
|---|---|---|
| `DATABASE_URL` | auto | public URL |
| `REDIS_URL` | auto | public URL |
| `ANTHROPIC_API_KEY` | **never** | only here |
| `OPENAI_API_KEY` | **never** | only here |

Provider keys exist in exactly one place. The sandbox gets a placeholder and the
runner's proxy attaches the real key upstream, so the least trusted process in
the system never holds the most valuable credential in it.

Nothing sensitive goes near the dashboard either: Next.js ships every
`NEXT_PUBLIC_` variable to the browser, and the dashboard has no need for a
provider key regardless.

## The Docker socket, stated plainly

The `runbox` account is in the `docker` group, which is effectively root on that
host. That is unavoidable — creating containers is the runner's entire job.

The mitigations are that the box runs nothing else, exposes no public HTTP
service, and only takes work off a queue. It is not a substitute for the microVM
isolation the main README names as out of scope; it is the reason that boundary
matters.
