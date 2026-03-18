"""Postgres access.

asyncpg directly rather than an ORM. The queries here are the interesting part
of the system — cursor pagination, a replay window, a rollup group-by — and
writing them as SQL keeps them legible instead of hiding them behind a query
builder.

Row-level security is set per connection with `set_config(..., is_local => true)`
so it is scoped to the transaction and cannot leak between pooled checkouts.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

from .config import Settings


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._settings.database_url,
            min_size=self._settings.db_pool_min,
            max_size=self._settings.db_pool_max,
            command_timeout=30,
            init=_init_connection,
        )

    async def disconnect(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("database pool is not initialised")
        return self._pool

    @asynccontextmanager
    async def acquire(self, tenant_id: str | None = None) -> AsyncIterator[asyncpg.Connection]:
        """Check out a connection with the tenant bound for RLS.

        Always inside a transaction, because `is_local => true` is what
        guarantees the setting is discarded when the connection returns to the
        pool. A tenant id that outlived its request would be the worst possible
        bug in a multi-tenant system.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            if tenant_id is not None:
                await conn.execute(
                    "select set_config('runbox.tenant_id', $1, true)", str(tenant_id)
                )
            yield conn

    @asynccontextmanager
    async def acquire_admin(self) -> AsyncIterator[asyncpg.Connection]:
        """A connection with no tenant bound, for auth lookups and health.

        The role used here owns the tables and therefore bypasses RLS. Keep the
        set of callers small and obvious.
        """
        async with self.pool.acquire() as conn:
            yield conn


async def _init_connection(conn: asyncpg.Connection) -> None:
    # asyncpg hands back jsonb as a string by default. Decoding it here means
    # every caller gets dicts rather than remembering to json.loads.
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


def record_to_dict(record: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(record) if record is not None else None
