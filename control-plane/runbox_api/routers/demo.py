"""The public demo.

No signup. If a hiring manager has to create an account, they will not.

The demo tenant is read-only in every sense that matters: these endpoints
resolve it server-side from a slug, so there is no demo API key to leak and
nothing a visitor sends can name a different tenant. The single write path —
starting an example run — is IP rate limited and restricted to a fixed list of
prompts.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import ratelimit
from ..auth import get_db
from ..bus import Bus
from ..config import Settings
from ..db import Database
from ..sse import SSE_HEADERS, replay_then_subscribe

logger = logging.getLogger("runbox.demo")

router = APIRouter(prefix="/v1/demo", tags=["demo"])

# A fixed menu, not free text. A public endpoint that runs arbitrary prompts on
# my credit card is a donation to whoever finds it first. These are chosen to
# show tool use and streaming without needing anything unusual.
EXAMPLES = [
    {
        "id": "releases",
        "label": "Summarise recent Go releases",
        "task": (
            "Fetch https://api.github.com/repos/golang/go/releases and summarise "
            "the three most recent releases in two sentences each."
        ),
        "tools": ["http_get"],
        "model": "claude-haiku-4-5",
    },
    {
        "id": "wikipedia",
        "label": "Explain a concept from Wikipedia",
        "task": (
            "Fetch https://en.wikipedia.org/wiki/CAP_theorem and explain the CAP "
            "theorem in plain language, in under 150 words."
        ),
        "tools": ["http_get"],
        "model": "claude-haiku-4-5",
    },
    {
        "id": "reasoning",
        "label": "A question with no tools",
        "task": (
            "A train leaves at 14:05 and arrives at 17:40, stopping for 12 minutes "
            "at one station. How long was it moving? Show your reasoning briefly."
        ),
        "tools": [],
        "model": "claude-haiku-4-5",
    },
]

EXAMPLES_BY_ID = {example["id"]: example for example in EXAMPLES}


class StartExampleRequest(BaseModel):
    example_id: str = Field(max_length=50)


async def get_bus(request: Request) -> Bus:
    return request.app.state.bus


async def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


async def _demo_tenant_id(db: Database, settings: Settings) -> str:
    async with db.acquire_admin() as conn:
        tenant_id = await conn.fetchval(
            "select id from tenants where slug = $1", settings.demo_tenant_slug
        )
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "demo_unavailable", "message": "The demo tenant is not configured."},
        )
    return str(tenant_id)


@router.get("/examples")
async def list_examples() -> dict[str, Any]:
    """The prompts a visitor may run. Fixed, deliberately."""
    return {
        "examples": [
            {"id": e["id"], "label": e["label"], "task": e["task"], "model": e["model"]}
            for e in EXAMPLES
        ]
    }


@router.get("/runs")
async def list_demo_runs(
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    limit: int = 20,
) -> dict[str, Any]:
    """Seeded and recent demo runs, so the page is never empty on arrival."""
    tenant_id = await _demo_tenant_id(db, settings)

    async with db.acquire(tenant_id) as conn:
        rows = await conn.fetch(
            """
            select r.id, r.status, r.task, r.model, r.tools, r.result,
                   r.created_at, r.duration_ms,
                   u.input_tokens, u.output_tokens, u.cost_micros
            from runs r
            left join usage_records u on u.run_id = r.id
            where r.tenant_id = $1
            order by r.created_at desc
            limit $2
            """,
            tenant_id,
            min(limit, 50),
        )

    return {
        "data": [
            {
                **dict(row),
                "id": str(row["id"]),
                "tools": list(row["tools"] or []),
            }
            for row in rows
        ]
    }


@router.get("/runs/{run_id}/events")
async def demo_run_events(
    run_id: str,
    db: Annotated[Database, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    after: int = 0,
) -> dict[str, Any]:
    """A demo run's trace, for replaying a seeded run without an API key."""
    tenant_id = await _demo_tenant_id(db, settings)

    async with db.acquire(tenant_id) as conn:
        owned = await conn.fetchval(
            "select 1 from runs where id = $1 and tenant_id = $2", run_id, tenant_id
        )
        if not owned:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": "Run does not exist."},
            )
        rows = await conn.fetch(
            """
            select seq, type, payload from trace_events
            where run_id = $1 and seq > $2 order by seq limit 2000
            """,
            run_id,
            after,
        )

    return {"data": [dict(row) for row in rows]}


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_example(
    body: StartExampleRequest,
    request: Request,
    response: Response,
    db: Annotated[Database, Depends(get_db)],
    bus: Annotated[Bus, Depends(get_bus)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> dict[str, str]:
    """Run one of the fixed examples. Rate limited by IP."""
    example = EXAMPLES_BY_ID.get(body.example_id)
    if example is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unknown_example",
                "message": f"Unknown example. Available: {', '.join(EXAMPLES_BY_ID)}.",
            },
        )

    ip = ratelimit.client_ip(request)
    verdict = await ratelimit.check(bus.redis, ip, settings.demo_rate_limit_per_hour)

    # Sent whether or not the request was allowed, so a client can pace itself
    # rather than discovering the limit by hitting it.
    response.headers.update(ratelimit.headers(verdict))

    if not verdict.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "demo_rate_limited",
                "message": (
                    f"The demo allows {verdict.limit} runs per hour. "
                    f"Try again in {verdict.retry_after // 60 + 1} minutes, or use an API key."
                ),
            },
            headers=ratelimit.headers(verdict),
        )

    tenant_id = await _demo_tenant_id(db, settings)

    async with db.acquire(tenant_id) as conn:
        row = await conn.fetchrow(
            """
            insert into runs
              (tenant_id, status, task, model, tools, timeout_s, max_tokens)
            values ($1, 'queued', $2, $3, $4, 90, 4000)
            returning id
            """,
            tenant_id,
            example["task"],
            example["model"],
            example["tools"],
        )

    run_id = str(row["id"])
    try:
        await bus.enqueue(run_id)
    except Exception:  # noqa: BLE001
        logger.exception("demo enqueue failed for %s; the sweep will pick it up", run_id)

    logger.info("demo run %s started for %s (%d left)", run_id, ip, verdict.remaining)
    return {"id": run_id, "status": "queued"}


@router.get("/runs/{run_id}/stream")
async def stream_demo_run(
    run_id: str,
    request: Request,
    db: Annotated[Database, Depends(get_db)],
    bus: Annotated[Bus, Depends(get_bus)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    after: int = 0,
) -> StreamingResponse:
    """Live trace for a demo run, with no API key.

    Reuses replay_then_subscribe rather than reimplementing it. The demo showing
    a *different* streaming path to the real one would make it a demo of
    something that does not exist.
    """
    tenant_id = await _demo_tenant_id(db, settings)

    async with db.acquire(tenant_id) as conn:
        owned = await conn.fetchval(
            "select 1 from runs where id = $1 and tenant_id = $2", run_id, tenant_id
        )
    if not owned:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": "Run does not exist."},
        )

    async def generate():
        yield ": open\n\n"
        async for frame in replay_then_subscribe(
            db=db, bus=bus, settings=settings, tenant_id=tenant_id, run_id=run_id, after=after
        ):
            if await request.is_disconnected():
                return
            yield frame

    return StreamingResponse(generate(), headers=SSE_HEADERS)
