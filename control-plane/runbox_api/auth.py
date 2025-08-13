"""API key authentication and tenant resolution.

Keys look like `rb_live_<32 url-safe chars>`. Only a SHA-256 of the key is
stored; the plaintext is shown once at creation and never again.

SHA-256 rather than bcrypt/argon2 is a deliberate choice and worth defending:
an API key is 192 bits of entropy from a CSPRNG, not a human-chosen password.
There is no dictionary to attack, so a slow KDF buys nothing and would put a
100ms delay on every single request.
"""

from __future__ import annotations

import contextlib
import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status

from .db import Database

KEY_PREFIX = "rb_live_"
KEY_BYTES = 24  # 32 url-safe characters
DISPLAY_PREFIX_LEN = len(KEY_PREFIX) + 4


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    tenant_slug: str
    tenant_name: str
    api_key_id: str
    read_only: bool = False


def generate_key() -> tuple[str, str, str]:
    """Return (plaintext, sha256 hash, display prefix)."""
    key = KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)
    return key, hash_key(key), key[:DISPLAY_PREFIX_LEN]


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _extract_bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def get_db(request: Request) -> Database:
    return request.app.state.db


async def authenticate(
    authorization: str | None = Header(default=None),
    db: Database = Depends(get_db),
) -> Principal:
    """Resolve an API key to a tenant, or 401."""
    key = _extract_bearer(authorization)
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "missing_credentials",
                "message": "Provide an API key as 'Authorization: Bearer rb_live_...'",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    async with db.acquire_admin() as conn:
        row = await conn.fetchrow(
            """
            select k.id as api_key_id, k.revoked_at,
                   t.id as tenant_id, t.slug as tenant_slug, t.name as tenant_name
            from api_keys k
            join tenants t on t.id = k.tenant_id
            where k.key_hash = $1
            """,
            hash_key(key),
        )

        # A revoked key and an unknown key give the same response. Telling a
        # caller "that key existed once" is information they have not earned.
        if row is None or row["revoked_at"] is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "invalid_api_key", "message": "API key is invalid or revoked."},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # last_used_at is for the dashboard, and its write should never be able
        # to fail a request that is otherwise fine.
        with contextlib.suppress(Exception):
            await conn.execute(
                "update api_keys set last_used_at = now() where id = $1", row["api_key_id"]
            )

    return Principal(
        tenant_id=str(row["tenant_id"]),
        tenant_slug=row["tenant_slug"],
        tenant_name=row["tenant_name"],
        api_key_id=str(row["api_key_id"]),
    )
