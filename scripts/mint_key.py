#!/usr/bin/env python3
"""Mint an API key for a tenant.

`seed.py` is for a development database: it creates fixed tenants and prints
both keys. This is the one to run against a real deployment, where you want one
key for one tenant and nothing else touched.

    DATABASE_URL=postgresql://... python scripts/mint_key.py dashboard --max-concurrent 4

Creates the tenant if it does not exist. The plaintext is printed once and is
not recoverable — only its SHA-256 is stored — so a lost key is replaced rather
than found.

Revoking is the same shape:

    DATABASE_URL=postgresql://... python scripts/mint_key.py --revoke rb_live_xxx
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import secrets
import sys

import asyncpg

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://runbox:runbox@localhost:5432/runbox")

KEY_PREFIX = "rb_live_"
KEY_BYTES = 24  # 32 url-safe characters


def mint() -> tuple[str, str, str]:
    key = KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)
    return key, hashlib.sha256(key.encode()).hexdigest(), key[:12]


async def create(
    conn: asyncpg.Connection, slug: str, label: str, max_concurrent: int | None
) -> str:
    tenant_id = await conn.fetchval(
        """
        insert into tenants (slug, name) values ($1, $2)
        on conflict (slug) do update set slug = excluded.slug
        returning id
        """,
        slug,
        slug.replace("-", " ").title(),
    )

    # A tenant with no `tenant_limits` row has no limits at all. That is the
    # right default for a key held by one person, and the wrong one for a key
    # the hosted dashboard hands to every visitor — there is a single runner
    # behind it, and concurrency is the resource it actually runs out of.
    if max_concurrent is not None:
        await conn.execute(
            """
            insert into tenant_limits (tenant_id, max_concurrent_runs)
            values ($1, $2)
            on conflict (tenant_id) do update
              set max_concurrent_runs = excluded.max_concurrent_runs,
                  updated_at = now()
            """,
            tenant_id,
            max_concurrent,
        )

    key, key_hash, prefix = mint()
    await conn.execute(
        "insert into api_keys (tenant_id, key_hash, key_prefix, name) values ($1, $2, $3, $4)",
        tenant_id,
        key_hash,
        prefix,
        label,
    )
    print(f"tenant {slug} ({tenant_id})")
    print(f"key    {key}")
    print("\nShown once. Only the hash is stored.")
    return key


async def revoke(conn: asyncpg.Connection, key: str) -> int:
    updated = await conn.execute(
        "update api_keys set revoked_at = now() where key_hash = $1 and revoked_at is null",
        hashlib.sha256(key.encode()).hexdigest(),
    )
    if updated.endswith(" 0"):
        # Unknown and already-revoked are the same outcome, and saying which is
        # not useful to anyone standing at a terminal.
        print("No active key matched.", file=sys.stderr)
        return 1
    print("Revoked.")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="tenant slug to mint for, or the key to revoke")
    parser.add_argument("--revoke", action="store_true", help="revoke the given key instead")
    parser.add_argument("--label", default="dashboard", help="label stored alongside the key")
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=None,
        help="cap simultaneous runs for this tenant (omit for no limit)",
    )
    args = parser.parse_args()

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        if args.revoke:
            return await revoke(conn, args.target)
        async with conn.transaction():
            await create(conn, args.target, args.label, args.max_concurrent)
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
