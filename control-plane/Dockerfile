FROM python:3.12-slim-bookworm AS build

# uv resolves and installs an order of magnitude faster than pip, which matters
# more in CI than locally.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /src
COPY pyproject.toml ./
COPY runbox_api ./runbox_api

RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache .

FROM python:3.12-slim-bookworm

RUN groupadd --gid 10001 runbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin runbox

COPY --from=build /opt/venv /opt/venv
COPY --chown=root:root runbox_api /app/runbox_api

WORKDIR /app
USER runbox

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

# One worker. The control plane is IO-bound and holds long-lived SSE
# connections; more workers would multiply memory without adding throughput,
# and horizontal scaling is a platform concern rather than a flag here.
CMD ["uvicorn", "runbox_api.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--timeout-keep-alive", "75"]
