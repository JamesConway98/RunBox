"""Opaque keyset cursors.

Offset pagination shifts under you whenever a row is inserted, and the runs
list is live-updating by design, so that is not a hypothetical problem. A
keyset cursor over `(created_at, id)` is stable regardless of what arrives
while the client is paging.

The base64 is not security — it is a signal to callers that the contents are
not part of the contract and will change.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime

from fastapi import HTTPException, status


def encode(created_at: datetime, row_id: str) -> str:
    raw = f"{created_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode(cursor: str) -> tuple[datetime, str]:
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding).decode()
        timestamp, _, row_id = raw.partition("|")
        if not row_id:
            raise ValueError("cursor is missing its id component")
        return datetime.fromisoformat(timestamp), row_id
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_cursor", "message": f"Cursor is not valid: {exc}"},
        ) from exc
