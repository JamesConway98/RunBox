#!/usr/bin/env python3
"""Seed a development database.

Creates two tenants — a normal one and the public read-only demo tenant — plus
an API key for each. Prints the plaintext keys once, which is the only time
they exist anywhere outside the caller's terminal.

Idempotent: re-running it does not duplicate tenants, and it mints a fresh key
each time rather than trying to recover one it cannot read back.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import sys

import asyncpg

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://runbox:runbox@localhost:5432/runbox"
)

KEY_PREFIX = "rb_live_"


def mint() -> tuple[str, str, str]:
    key = KEY_PREFIX + secrets.token_urlsafe(24)
    return key, hashlib.sha256(key.encode()).hexdigest(), key[:12]


async def upsert_tenant(conn: asyncpg.Connection, slug: str, name: str) -> str:
    return await conn.fetchval(
        """
        insert into tenants (slug, name) values ($1, $2)
        on conflict (slug) do update set name = excluded.name
        returning id
        """,
        slug,
        name,
    )


async def add_key(conn: asyncpg.Connection, tenant_id: str, label: str) -> str:
    key, key_hash, prefix = mint()
    await conn.execute(
        "insert into api_keys (tenant_id, key_hash, key_prefix, name) values ($1, $2, $3, $4)",
        tenant_id,
        key_hash,
        prefix,
        label,
    )
    return key


async def main() -> int:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        async with conn.transaction():
            acme = await upsert_tenant(conn, "acme", "Acme Inc")
            demo = await upsert_tenant(conn, "demo", "Public Demo")

            acme_key = await add_key(conn, acme, "seed key")
            demo_key = await add_key(conn, demo, "demo key")

        print("Seeded.\n")
        print(f"  acme  {acme}\n        {acme_key}\n")
        print(f"  demo  {demo}\n        {demo_key}\n")
        print("These are shown once. Only the hashes are stored.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
