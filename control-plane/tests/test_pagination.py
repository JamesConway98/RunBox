from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from runbox_api import pagination


def test_round_trip():
    when = datetime(2025, 8, 23, 14, 5, 9, 123456, tzinfo=UTC)
    row_id = "0199c0de-1234-4a5b-8c9d-000000000001"

    decoded_at, decoded_id = pagination.decode(pagination.encode(when, row_id))

    assert decoded_at == when
    assert decoded_id == row_id


def test_cursor_is_url_safe():
    # Cursors travel in query strings. A '+' or '/' would need escaping and
    # would eventually be mangled by something in the middle.
    cursor = pagination.encode(datetime.now(UTC), "abc-123")
    assert "+" not in cursor
    assert "/" not in cursor
    assert "=" not in cursor  # padding is stripped, and decode restores it


@pytest.mark.parametrize(
    "bad",
    [
        "not-base64!!",
        "",
        "YWJj",  # decodes, but has no '|' separator
        "fHRlc3Q=",  # empty timestamp component
    ],
)
def test_invalid_cursors_are_400_not_500(bad):
    with pytest.raises(HTTPException) as exc:
        pagination.decode(bad)

    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "invalid_cursor"
