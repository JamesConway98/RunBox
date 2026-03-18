from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from .. import pagination, quotas
from ..auth import Principal, authenticate, get_db
from ..bus import Bus
from ..config import Settings
from ..db import Database
from ..schemas import (
    TERMINAL_STATUSES,
    CreateRunRequest,
    EventList,
    Run,
    RunCreated,
    RunList,
    TraceEvent,
    Usage,
)
from ..sse import SSE_HEADERS, replay_then_subscribe

logger = logging.getLogger("runbox.runs")

router = APIRouter(prefix="/v1/runs", tags=["runs"])


async def get_bus(request: Request) -> Bus:
    return request.app.state.bus


async def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


RUN_COLUMNS = """
    r.id, r.status, r.task, r.model, r.tools, r.result, r.error,
    r.created_at, r.started_at, r.finished_at, r.duration_ms
"""


def _to_run(row) -> Run:
    data = dict(row)
    usage = None
    if data.pop("has_usage", False):
        usage = Usage(
            input_tokens=data.pop("input_tokens", 0) or 0,
            output_tokens=data.pop("output_tokens", 0) or 0,
            tool_calls=data.pop("tool_calls", 0) or 0,
            compute_ms=data.pop("compute_ms", 0) or 0,
            cost_micros=data.pop("cost_micros", 0) or 0,
        )
    for key in ("input_tokens", "output_tokens", "tool_calls", "compute_ms", "cost_micros"):
        data.pop(key, None)
    data["id"] = str(data["id"])
    data["tools"] = list(data.get("tools") or [])
    return Run(**data, usage=usage)


@router.post("", response_model=RunCreated, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    body: CreateRunRequest,
    response: Response,
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    bus: Annotated[Bus, Depends(get_bus)],
) -> RunCreated:
    """Create and enqueue a run.

    202 rather than 201: the run exists, but nothing has happened yet. The
    caller should watch the stream or poll the detail endpoint.
    """
    async with db.acquire(principal.tenant_id) as conn:
        # Before anything else. A run rejected before a container exists costs
        # nothing; one killed halfway has already burned tokens.
        await quotas.enforce(conn, principal.tenant_id)

        model = await conn.fetchrow(
            "select model, supports_tools from model_pricing where model = $1 and active",
            body.model,
        )
        if model is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "unknown_model",
                    "message": f"Model '{body.model}' is not available.",
                },
            )
        if body.tools and not model["supports_tools"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "tools_unsupported",
                    "message": f"Model '{body.model}' does not support tool calling.",
                },
            )

        row = await conn.fetchrow(
            """
            insert into runs (
                tenant_id, status, task, model, tools,
                system_prompt, temperature, timeout_s, max_tokens
            )
            values ($1, 'queued', $2, $3, $4, $5, $6, $7, $8)
            returning id, status
            """,
            principal.tenant_id,
            body.task,
            body.model,
            body.tools,
            body.system_prompt,
            body.temperature,
            body.timeout_s,
            body.max_tokens,
        )

    run_id = str(row["id"])

    # Enqueue after the row is committed, never before. A queue entry pointing
    # at a row that does not exist yet is a race the runner would have to
    # defend against; a row with no queue entry is just a run the runner's
    # Postgres sweep picks up a moment later.
    try:
        await bus.enqueue(run_id)
    except Exception:  # noqa: BLE001
        logger.exception("enqueue failed for run %s; falling back to the sweep", run_id)

    response.headers["Location"] = f"/v1/runs/{run_id}"
    return RunCreated(id=run_id, status=row["status"])


@router.get("", response_model=RunList)
async def list_runs(
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    model: str | None = Query(default=None),
) -> RunList:
    conditions = ["r.tenant_id = $1"]
    params: list = [principal.tenant_id]

    if cursor:
        created_at, row_id = pagination.decode(cursor)
        params.extend([created_at, row_id])
        # Tie-break on id so runs created in the same microsecond cannot be
        # skipped or repeated across pages.
        conditions.append(f"(r.created_at, r.id) < (${len(params) - 1}, ${len(params)})")

    if status_filter:
        params.append([s.strip() for s in status_filter.split(",") if s.strip()])
        conditions.append(f"r.status = any(${len(params)})")

    if model:
        params.append(model)
        conditions.append(f"r.model = ${len(params)}")

    params.append(limit + 1)  # over-fetch by one to learn has_more without a count
    query = f"""
        select {RUN_COLUMNS},
               u.run_id is not null as has_usage,
               u.input_tokens, u.output_tokens, u.tool_calls, u.compute_ms, u.cost_micros
        from runs r
        left join usage_records u on u.run_id = r.id
        where {" and ".join(conditions)}
        order by r.created_at desc, r.id desc
        limit ${len(params)}
    """

    async with db.acquire(principal.tenant_id) as conn:
        rows = await conn.fetch(query, *params)

    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = None
    if has_more and rows:
        next_cursor = pagination.encode(rows[-1]["created_at"], str(rows[-1]["id"]))
    return RunList(data=[_to_run(r) for r in rows], has_more=has_more, next_cursor=next_cursor)


@router.get("/{run_id}", response_model=Run)
async def get_run(
    run_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
) -> Run:
    async with db.acquire(principal.tenant_id) as conn:
        row = await conn.fetchrow(
            f"""
            select {RUN_COLUMNS},
                   u.run_id is not null as has_usage,
                   u.input_tokens, u.output_tokens, u.tool_calls, u.compute_ms, u.cost_micros
            from runs r
            left join usage_records u on u.run_id = r.id
            where r.id = $1 and r.tenant_id = $2
            """,
            run_id,
            principal.tenant_id,
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"Run '{run_id}' does not exist."},
        )
    return _to_run(row)


@router.get("/{run_id}/events", response_model=EventList)
async def list_events(
    run_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    after: int = Query(default=0, ge=0, description="Return events with seq greater than this"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> EventList:
    """The full trace, paginated by seq.

    `after` is the same cursor the streaming endpoint uses, so a client can move
    between the two without translating anything.
    """
    async with db.acquire(principal.tenant_id) as conn:
        exists = await conn.fetchval(
            "select 1 from runs where id = $1 and tenant_id = $2", run_id, principal.tenant_id
        )
        if not exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": f"Run '{run_id}' does not exist."},
            )

        rows = await conn.fetch(
            """
            select seq, type, payload, created_at
            from trace_events
            where run_id = $1 and seq > $2
            order by seq
            limit $3
            """,
            run_id,
            after,
            limit + 1,
        )

    has_more = len(rows) > limit
    rows = rows[:limit]
    return EventList(
        data=[TraceEvent(**dict(r)) for r in rows],
        has_more=has_more,
        next_cursor=str(rows[-1]["seq"]) if has_more and rows else None,
    )


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: str,
    request: Request,
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    bus: Annotated[Bus, Depends(get_bus)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    after: int = Query(default=0, ge=0, description="Resume from this seq"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Live trace as server-sent events.

    Resume from a cursor either explicitly with `?after=`, or implicitly — the
    browser's EventSource replays `Last-Event-ID` on its own reconnect, and
    honouring it means resumption costs the client nothing at all.

    The explicit parameter wins when both are present, because a caller that
    said something specific meant it.
    """
    if after == 0 and last_event_id:
        try:
            after = max(0, int(last_event_id))
        except ValueError:
            # A header we cannot parse is not worth a 400. Start from the top;
            # the client gets a complete trace rather than an error.
            logger.warning("unparseable Last-Event-ID: %r", last_event_id)

    async with db.acquire(principal.tenant_id) as conn:
        exists = await conn.fetchval(
            "select 1 from runs where id = $1 and tenant_id = $2", run_id, principal.tenant_id
        )
    if not exists:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"Run '{run_id}' does not exist."},
        )

    async def generate():
        # Flush a comment immediately. It gets headers on the wire before the
        # first event, so the client's onopen fires now rather than whenever
        # the agent happens to say something.
        yield ": open\n\n"
        async for frame in replay_then_subscribe(
            db=db,
            bus=bus,
            settings=settings,
            tenant_id=principal.tenant_id,
            run_id=run_id,
            after=after,
        ):
            if await request.is_disconnected():
                # The run carries on. Streaming is observation, not control.
                logger.debug("client disconnected from run %s", run_id)
                return
            yield frame

    return StreamingResponse(generate(), headers=SSE_HEADERS)


@router.post("/{run_id}/cancel", response_model=Run)
async def cancel_run(
    run_id: str,
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    bus: Annotated[Bus, Depends(get_bus)],
) -> Run:
    """Request cooperative cancellation.

    Idempotent: cancelling an already-terminal run returns it unchanged rather
    than erroring, because a client retrying a cancel is doing the right thing
    and should not be punished for it.
    """
    async with db.acquire(principal.tenant_id) as conn:
        row = await conn.fetchrow(
            "select status from runs where id = $1 and tenant_id = $2",
            run_id,
            principal.tenant_id,
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "message": f"Run '{run_id}' does not exist."},
            )

        if row["status"] not in TERMINAL_STATUSES:
            # A queued run can be cancelled here and now; nothing has started.
            # A running one needs the runner to notice, so we only flag it and
            # let the worker record the terminal state.
            if row["status"] == "queued":
                await conn.execute(
                    """
                    update runs set status = 'cancelled', finished_at = now(), duration_ms = 0
                    where id = $1 and status = 'queued'
                    """,
                    run_id,
                )
            await bus.request_cancel(run_id)

    return await get_run(run_id, principal, db)
