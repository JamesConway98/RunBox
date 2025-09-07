"""Quota enforcement at enqueue time.

Checking here rather than mid-run is the whole point. A run that is rejected
before a container exists costs nothing; one killed halfway has already burned
tokens that someone has to account for.

The check is advisory in one specific sense worth being honest about: between
the check and the run's completion, concurrent runs can push a tenant over
their ceiling. Closing that gap properly means reserving budget at enqueue and
settling it at completion, which is real work for a limit that only needs to be
approximately right. The overshoot is bounded by max_concurrent_runs, and that
is a deliberate trade rather than an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg
from fastapi import HTTPException, status


@dataclass(frozen=True)
class Limits:
    monthly_token_ceiling: int | None
    monthly_cost_ceiling_micros: int | None
    max_concurrent_runs: int


@dataclass(frozen=True)
class Consumption:
    total_tokens: int
    total_cost_micros: int
    run_count: int
    active_runs: int


async def load_limits(conn: asyncpg.Connection, tenant_id: str) -> Limits | None:
    row = await conn.fetchrow(
        """
        select monthly_token_ceiling, monthly_cost_ceiling_micros, max_concurrent_runs
        from tenant_limits where tenant_id = $1
        """,
        tenant_id,
    )
    if row is None:
        return None  # no row means no limits, which is the default for a tenant
    return Limits(**dict(row))


async def load_consumption(conn: asyncpg.Connection, tenant_id: str) -> Consumption:
    usage = await conn.fetchrow("select * from runbox_month_usage($1)", tenant_id)
    active = await conn.fetchval(
        "select count(*) from runs where tenant_id = $1 and status in ('queued','running')",
        tenant_id,
    )
    return Consumption(
        total_tokens=usage["total_tokens"],
        total_cost_micros=usage["total_cost_micros"],
        run_count=usage["run_count"],
        active_runs=int(active or 0),
    )


async def enforce(conn: asyncpg.Connection, tenant_id: str) -> None:
    """Raise 429 when the tenant is over a ceiling.

    429 rather than 403: the request is well-formed and the caller is
    authorised, they have simply used their allowance. That distinction matters
    because a client library should back off on one and stop on the other.
    """
    limits = await load_limits(conn, tenant_id)
    if limits is None:
        return

    used = await load_consumption(conn, tenant_id)

    if used.active_runs >= limits.max_concurrent_runs:
        raise _quota_error(
            "concurrency_limit",
            f"{used.active_runs} runs are already queued or running "
            f"(limit {limits.max_concurrent_runs}). Wait for one to finish.",
            retry_after=10,
        )

    ceiling = limits.monthly_token_ceiling
    if ceiling is not None and used.total_tokens >= ceiling:
        raise _quota_error(
            "token_quota_exceeded",
            f"Used {used.total_tokens:,} of {ceiling:,} tokens this month.",
        )

    cost_ceiling = limits.monthly_cost_ceiling_micros
    if cost_ceiling is not None and used.total_cost_micros >= cost_ceiling:
        raise _quota_error(
            "cost_quota_exceeded",
            f"Used ${used.total_cost_micros / 1_000_000:.2f} of "
            f"${cost_ceiling / 1_000_000:.2f} this month.",
        )


def _quota_error(code: str, message: str, retry_after: int | None = None) -> HTTPException:
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"error": code, "message": message},
        headers=headers,
    )
