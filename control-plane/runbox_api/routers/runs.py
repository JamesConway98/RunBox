from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from .. import pagination
from ..auth import Principal, authenticate, get_db
from ..db import Database
from ..schemas import (
    CreateRunRequest,
    EventList,
    Run,
    RunCreated,
    RunList,
    TraceEvent,
    Usage,
)

router = APIRouter(prefix="/v1/runs", tags=["runs"])

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
) -> RunCreated:
    """Create and enqueue a run.

    202 rather than 201: the run exists, but nothing has happened yet. The
    caller should watch the stream or poll the detail endpoint.
    """
    async with db.acquire(principal.tenant_id) as conn:
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
        where {' and '.join(conditions)}
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
