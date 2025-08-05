# The sandbox image. Everything the agent needs and nothing else.
#
# The hardening that actually matters is applied by the runner at container
# creation time (dropped capabilities, no network, read-only rootfs, resource
# limits). What this file contributes is a small surface and a non-root user,
# so that those restrictions are not the only thing standing in the way.

FROM python:3.12-slim-bookworm

# Dependencies first so the layer caches independently of the agent source.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes=false -r /tmp/requirements.txt \
    && rm -rf /root/.cache /tmp/requirements.txt

# uid 10001 is arbitrary but fixed: the runner mounts the workspace with it.
RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin agent \
    && mkdir -p /app /workspace /run/runbox \
    && chown -R agent:agent /workspace

COPY --chown=root:root agent.py llm.py tools.py trace.py /app/

WORKDIR /app
USER agent

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RUNBOX_WORKSPACE=/workspace

# No shell form: PID 1 should be python so that SIGTERM reaches the agent and
# its handler can emit a cancelled trace event before the container dies.
ENTRYPOINT ["python", "-u", "/app/agent.py"]
