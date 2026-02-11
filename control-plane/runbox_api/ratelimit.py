"""IP rate limiting for the public demo.

A sliding-window counter in Redis. Not a token bucket, and not because a bucket
would be wrong — because the thing being limited is "how many free runs can a
stranger start in an hour", and a plain counter with a TTL answers that
question exactly while a bucket needs state per key that has to be read,
mutated and written back.

The window is fixed rather than truly sliding. A caller who times it right gets
2N runs across a boundary. For a demo whose purpose is to let people try the
product, that is fine and the simplicity is worth more than the precision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Request

logger = logging.getLogger("runbox.ratelimit")

WINDOW_SECONDS = 3600
KEY_PREFIX = "runbox:ratelimit:demo:"


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    remaining: int
    limit: int
    retry_after: int


def client_ip(request: Request) -> str:
    """Best-effort client address.

    X-Forwarded-For is only trusted because this runs behind a proxy that sets
    it. If it were reachable directly, this header would be attacker-controlled
    and the limit trivially bypassed — worth stating plainly rather than leaving
    as an assumption.

    The *first* entry is the original client; later ones are the proxy chain.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


async def check(redis, ip: str, limit: int) -> Verdict:
    """Consume one unit of the caller's hourly allowance.

    INCR then EXPIRE, pipelined. INCR returns the post-increment value, so the
    first call in a window returns 1 and is the one that sets the TTL — no
    separate existence check, and no window that never expires because the
    EXPIRE raced with a concurrent INCR.
    """
    key = f"{KEY_PREFIX}{ip}"

    try:
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = await pipe.execute()

        if count == 1 or ttl < 0:
            await redis.expire(key, WINDOW_SECONDS)
            ttl = WINDOW_SECONDS
    except Exception:  # noqa: BLE001
        # Fail open. Redis being down should degrade the demo's protection, not
        # take the demo offline. The blast radius of failing open here is a few
        # extra free runs; the blast radius of failing closed is the public
        # demo returning 500s to every visitor.
        logger.exception("rate limit check failed for %s; allowing", ip)
        return Verdict(allowed=True, remaining=limit, limit=limit, retry_after=0)

    remaining = max(0, limit - count)
    return Verdict(
        allowed=count <= limit,
        remaining=remaining,
        limit=limit,
        retry_after=int(ttl) if ttl > 0 else WINDOW_SECONDS,
    )


def headers(verdict: Verdict) -> dict[str, str]:
    """Standard rate-limit headers.

    Sent on success as well as on rejection, so a client can pace itself rather
    than discovering the limit by hitting it.
    """
    values = {
        "X-RateLimit-Limit": str(verdict.limit),
        "X-RateLimit-Remaining": str(verdict.remaining),
    }
    if not verdict.allowed:
        values["Retry-After"] = str(verdict.retry_after)
    return values
