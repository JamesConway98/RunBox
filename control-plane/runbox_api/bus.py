"""Redis: the run queue and the live event bus.

The control plane only ever pushes to the queue; the runner is the only
consumer. On the pub/sub side the relationship is reversed.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import redis.asyncio as aioredis

from .config import Settings

logger = logging.getLogger("runbox.bus")

QUEUE_KEY = "runbox:queue:runs"
CHANNEL_PREFIX = "runbox:run:"
CANCEL_KEY = "runbox:cancel"


class Bus:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        self._redis = aioredis.from_url(
            self._settings.redis_url,
            decode_responses=True,
            health_check_interval=30,
        )
        await self._redis.ping()

    async def disconnect(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    @property
    def redis(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("redis is not connected")
        return self._redis

    async def enqueue(self, run_id: str) -> None:
        """Hand a run id to the runner.

        LPUSH pairs with the runner's BRPOP to give FIFO. If this fails the run
        row still exists as `queued`, and the runner's Postgres fallback will
        pick it up on its next sweep — so a Redis blip delays a run rather than
        losing it.
        """
        await self.redis.lpush(QUEUE_KEY, run_id)

    async def request_cancel(self, run_id: str) -> None:
        """Flag a run for cancellation.

        A set entry rather than a published message, because a cancel sent
        while the runner is reconnecting would simply vanish. The runner
        removes the entry once it has acted.
        """
        await self.redis.sadd(CANCEL_KEY, run_id)
        # Cancels for runs that never get picked up should not accumulate.
        await self.redis.expire(CANCEL_KEY, 3600)

    async def queue_depth(self) -> int:
        return int(await self.redis.llen(QUEUE_KEY))

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        """Subscribe to one run's live events.

        Yields an async iterator of decoded events. The subscription is
        established before the caller replays from Postgres, which is what
        closes the gap between "what is already durable" and "what arrives
        next".
        """
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(CHANNEL_PREFIX + run_id)
        try:
            yield _decode_messages(pubsub)
        finally:
            try:
                await pubsub.unsubscribe(CHANNEL_PREFIX + run_id)
            finally:
                await pubsub.aclose()


async def _decode_messages(pubsub) -> AsyncIterator[dict[str, Any]]:
    async for message in pubsub.listen():
        if message is None or message.get("type") != "message":
            continue
        try:
            yield json.loads(message["data"])
        except (ValueError, KeyError):
            # A message we cannot parse is one the runner should not have sent.
            # Drop it rather than tearing down a live stream over it.
            logger.warning("undecodable pub/sub message on %s", message.get("channel"))
            continue
