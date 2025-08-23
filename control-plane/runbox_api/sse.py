"""Server-sent events.

SSE rather than WebSockets, and the reasons are worth stating because picking
the boring option for the right reason is the interesting part:

- The data flows one way. A trace is observation, not control. A duplex
  protocol would be paying connection complexity for a direction we never use.
- Reconnection is native. The browser reconnects on its own and replays
  `Last-Event-ID`, so resumption is a header rather than a hand-rolled
  handshake.
- It survives proxies. SSE is ordinary chunked HTTP. WebSocket upgrades are
  still mangled by enough intermediaries to matter.

The hard part is not the transport, it is the join between what is already
durable in Postgres and what is about to arrive on Redis. That is
`replay_then_subscribe` below.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from .bus import Bus
from .config import Settings
from .db import Database
from .schemas import TERMINAL_STATUSES

logger = logging.getLogger("runbox.sse")

SSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # nginx buffers text/event-stream by default, which turns a live trace into
    # a batch delivered at the end. This is the header that stops it.
    "X-Accel-Buffering": "no",
}

# Bounds how far the live subscription may run ahead of a slow client. Beyond
# this the pump applies backpressure rather than growing without limit; the
# events are durable in Postgres regardless, so the cost of falling behind is
# latency, never data.
INBOX_MAX = 1000

_CLOSED = object()


def format_event(seq: int, event_type: str, payload: dict[str, Any]) -> str:
    """Serialise one SSE frame.

    `id:` carries the seq, which is what the browser echoes back as
    `Last-Event-ID` on reconnect. That single line is the whole resumption
    mechanism.
    """
    body = json.dumps(payload, separators=(",", ":"), default=str)
    return f"id: {seq}\nevent: {event_type}\ndata: {body}\n\n"


def format_comment(text: str) -> str:
    """A comment frame. Keeps the connection warm without the client seeing it."""
    return f": {text}\n\n"


async def replay_then_subscribe(
    *,
    db: Database,
    bus: Bus,
    settings: Settings,
    tenant_id: str,
    run_id: str,
    after: int,
) -> AsyncIterator[str]:
    """Stream a run's trace from `after` onwards, live, with no gaps.

    The ordering below is the entire point, and it is easy to get backwards:

    1. Subscribe to Redis *first*, pumping arrivals into a buffer.
    2. Then replay from Postgres, which is the durable record.
    3. Then drain the buffer, discarding anything the replay already covered.
    4. Then stream live.

    Subscribing after the replay would drop every event produced during the
    replay query. Replaying after going live would deliver them out of order.
    Deduplication is by `seq` — unique per run and monotonic — so "already
    covered" is a comparison rather than a guess.
    """
    async with bus.subscribe(run_id) as live:
        inbox: asyncio.Queue = asyncio.Queue(maxsize=INBOX_MAX)

        # A single consumer owns the subscription for its whole lifetime. Two
        # consumers taking turns on one async generator is a good way to close
        # it out from under yourself.
        async def pump() -> None:
            try:
                async for event in live:
                    await inbox.put(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("live subscription failed for run %s", run_id)
            finally:
                with contextlib.suppress(asyncio.QueueFull):
                    inbox.put_nowait(_CLOSED)

        pump_task = asyncio.create_task(pump())
        last_seq = after

        try:
            # --- 2. replay the durable record --------------------------------
            async for event in _replay(db, settings, tenant_id, run_id, after):
                last_seq = max(last_seq, event["seq"])
                yield format_event(event["seq"], event["type"], event["payload"])

            # --- 3. drain what arrived while we were replaying ---------------
            while True:
                try:
                    event = inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if event is _CLOSED:
                    return
                if event["seq"] <= last_seq:
                    continue  # the replay already covered it
                last_seq = event["seq"]
                yield format_event(event["seq"], event["type"], _payload_of(event))
                if event["type"] == "final":
                    return

            # A run that already finished has nothing live to wait for. Without
            # this check the client holds an open connection until it times out
            # for no reason.
            if await _is_terminal(db, tenant_id, run_id):
                yield format_comment("run already complete")
                return

            # --- 4. live ------------------------------------------------------
            while True:
                try:
                    event = await asyncio.wait_for(
                        inbox.get(), timeout=settings.sse_heartbeat_s
                    )
                except TimeoutError:
                    # Quiet stretches are normal — a run can spend 40 seconds
                    # inside one tool call. Without this the connection looks
                    # dead to every proxy between here and the client.
                    yield format_comment("heartbeat")
                    continue

                if event is _CLOSED:
                    return
                if event["seq"] <= last_seq:
                    continue
                last_seq = event["seq"]
                yield format_event(event["seq"], event["type"], _payload_of(event))
                if event["type"] == "final":
                    return

        finally:
            # A client that disconnects mid-run must not take the run with it.
            # Streaming is observation, not control; all that dies here is the
            # subscription.
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump_task


async def _replay(
    db: Database, settings: Settings, tenant_id: str, run_id: str, after: int
) -> AsyncIterator[dict[str, Any]]:
    """Page through the durable trace so a long run does not load in one go."""
    cursor = after
    while True:
        async with db.acquire(tenant_id) as conn:
            rows = await conn.fetch(
                """
                select seq, type, payload
                from trace_events
                where run_id = $1 and seq > $2
                order by seq
                limit $3
                """,
                run_id,
                cursor,
                settings.sse_replay_page,
            )
        if not rows:
            return
        for row in rows:
            yield {"seq": row["seq"], "type": row["type"], "payload": row["payload"]}
        cursor = rows[-1]["seq"]
        if len(rows) < settings.sse_replay_page:
            return


async def _is_terminal(db: Database, tenant_id: str, run_id: str) -> bool:
    async with db.acquire(tenant_id) as conn:
        status = await conn.fetchval(
            "select status from runs where id = $1 and tenant_id = $2", run_id, tenant_id
        )
    return status in TERMINAL_STATUSES


def _payload_of(event: dict[str, Any]) -> dict[str, Any]:
    """Normalise a pub/sub message into the payload clients see.

    The runner wraps the agent's raw line in an envelope; clients only care
    about the inside of it.
    """
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except ValueError:
            return {"raw": payload}
    return {}
