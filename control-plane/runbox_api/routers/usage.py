from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from ..auth import Principal, authenticate, get_db
from ..db import Database

router = APIRouter(prefix="/v1/usage", tags=["usage"])

GroupBy = Literal["day", "model", "total"]

# A cap on the window, not on the result size. A tenant asking for five years of
# daily rows is almost certainly a mistake, and the honest failure is a 400
# rather than a query that takes a minute and then times out behind a proxy.
MAX_WINDOW_DAYS = 400


class UsageBucket(BaseModel):
    key: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tool_calls: int
    compute_ms: int
    cost_micros: int
    run_count: int

    @property
    def cost_usd(self) -> float:
        return self.cost_micros / 1_000_000


class UsageResponse(BaseModel):
    group_by: GroupBy
    # Named start/end rather than from/to: `from` is a Python keyword, and a
    # field called `from_` leaking into the JSON is worse than a clear rename.
    # The query parameters keep the `from`/`to` spelling callers expect.
    start: date
    end: date
    buckets: list[UsageBucket]
    total: UsageBucket


# Aggregating in SQL rather than in Python. It is one group-by over an indexed
# range, and pulling every row across the wire to sum it in the API would be
# slower and would scale worse for no gain in clarity.
AGGREGATE = """
    coalesce(sum(input_tokens), 0)::bigint             as input_tokens,
    coalesce(sum(output_tokens), 0)::bigint            as output_tokens,
    coalesce(sum(input_tokens + output_tokens), 0)::bigint as total_tokens,
    coalesce(sum(tool_calls), 0)::bigint               as tool_calls,
    coalesce(sum(compute_ms), 0)::bigint               as compute_ms,
    coalesce(sum(cost_micros), 0)::bigint              as cost_micros,
    count(*)::bigint                                   as run_count
"""


@router.get("", response_model=UsageResponse)
async def get_usage(
    principal: Annotated[Principal, Depends(authenticate)],
    db: Annotated[Database, Depends(get_db)],
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = Query(default=None),
    group_by: GroupBy = Query(default="day"),
) -> UsageResponse:
    """Usage rollup for the authenticated tenant.

    A plain `date_trunc` group-by, which is entirely adequate at this scale. The
    next step at volume is a materialised view or a rollup table written on
    completion — deliberately not built yet, because doing so before the query
    is actually slow is how you end up maintaining a cache nobody needed.
    """
    end = to or datetime.now(UTC).date()
    start = from_ or (end - timedelta(days=29))

    if start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_range", "message": "'from' must not be after 'to'."},
        )
    if (end - start).days > MAX_WINDOW_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "range_too_large",
                "message": f"Window must be {MAX_WINDOW_DAYS} days or fewer.",
            },
        )

    # The range is half-open on the upper end so that a run at 23:59 on the last
    # day is included. A `created_at <= to` predicate silently drops most of the
    # final day, which is the kind of off-by-one that shows up as "yesterday's
    # numbers changed overnight".
    upper = end + timedelta(days=1)

    if group_by == "day":
        query = f"""
            select to_char(date_trunc('day', created_at), 'YYYY-MM-DD') as key, {AGGREGATE}
            from usage_records
            where tenant_id = $1 and created_at >= $2 and created_at < $3
            group by 1
            order by 1
        """
    elif group_by == "model":
        query = f"""
            select model as key, {AGGREGATE}
            from usage_records
            where tenant_id = $1 and created_at >= $2 and created_at < $3
            group by 1
            order by cost_micros desc
        """
    else:
        query = f"""
            select 'total' as key, {AGGREGATE}
            from usage_records
            where tenant_id = $1 and created_at >= $2 and created_at < $3
        """

    async with db.acquire(principal.tenant_id) as conn:
        rows = await conn.fetch(query, principal.tenant_id, start, upper)

    buckets = [UsageBucket(**dict(row)) for row in rows]
    return UsageResponse(
        group_by=group_by,
        start=start,
        end=end,
        buckets=buckets,
        total=_sum(buckets),
    )


def _sum(buckets: list[UsageBucket]) -> UsageBucket:
    """Total across buckets, computed here rather than by a second query.

    The numbers are already in memory and summing them again in Postgres would
    be a round trip to re-derive something we hold.
    """
    return UsageBucket(
        key="total",
        input_tokens=sum(b.input_tokens for b in buckets),
        output_tokens=sum(b.output_tokens for b in buckets),
        total_tokens=sum(b.total_tokens for b in buckets),
        tool_calls=sum(b.tool_calls for b in buckets),
        compute_ms=sum(b.compute_ms for b in buckets),
        cost_micros=sum(b.cost_micros for b in buckets),
        run_count=sum(b.run_count for b in buckets),
    )
