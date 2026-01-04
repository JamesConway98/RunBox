"""SSE parsing and automatic resumption.

Two things this module does that a naive implementation skips, and both are the
reason streaming is worth having in an SDK rather than leaving to the caller:

1. **Reconnects transparently.** A dropped connection reconnects from the last
   `seq` seen, so the generator the caller is iterating simply keeps yielding.
   The server replays from Postgres then switches to live, so there is no gap
   and no duplicate.

2. **Never yields the same event twice.** The high-water mark is enforced here
   as well as on the server. Belt and braces, but the cost is one integer
   comparison and the failure it prevents is a caller double-counting tokens.
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any, Callable, Iterator

import httpx

from .errors import StreamError
from .types import Event

# Reconnect backoff. Short enough that a blip is invisible, capped so a genuine
# outage does not become a hot loop.
INITIAL_BACKOFF = 0.5
MAX_BACKOFF = 8.0
MAX_ATTEMPTS = 6


def parse_sse(lines: Iterator[str]) -> Iterator[tuple[int | None, str, dict[str, Any]]]:
    """Parse an SSE byte stream into (id, event, data) triples.

    A frame accumulates across lines and is dispatched on a blank line. Comment
    lines starting with ':' are heartbeats and are discarded — but reaching one
    still proves the connection is alive, which is exactly what they are for.
    """
    event_id: int | None = None
    event_type = "message"
    data_parts: list[str] = []

    for raw in lines:
        line = raw.rstrip("\n").rstrip("\r")

        if not line:
            if data_parts:
                payload = "\n".join(data_parts)
                # A frame we cannot parse is one we should not act on. Skipping
                # it keeps a single malformed event from killing the stream.
                with contextlib.suppress(ValueError):
                    yield event_id, event_type, json.loads(payload)
            event_id, event_type, data_parts = None, "message", []
            continue

        if line.startswith(":"):
            continue

        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value

        if field == "id":
            try:
                event_id = int(value)
            except ValueError:
                event_id = None
        elif field == "event":
            event_type = value
        elif field == "data":
            data_parts.append(value)


class EventStream:
    """An iterator over a run's trace that survives a dropped connection."""

    def __init__(
        self,
        client: httpx.Client,
        url: str,
        headers: dict[str, str],
        *,
        after: int = 0,
        max_attempts: int = MAX_ATTEMPTS,
        on_reconnect: Callable[[int, int], None] | None = None,
    ) -> None:
        self._client = client
        self._url = url
        self._headers = headers
        self._last_seq = after
        self._max_attempts = max_attempts
        self._on_reconnect = on_reconnect
        self._finished = False

    @property
    def last_seq(self) -> int:
        """Where the stream got to. Useful for resuming in a later process."""
        return self._last_seq

    def __iter__(self) -> Iterator[Event]:
        attempt = 0

        while not self._finished:
            try:
                yield from self._read_once()
                # A clean end without a final event means the server closed the
                # connection early. Reconnecting picks up where we left off.
                if not self._finished:
                    attempt += 1
                    if attempt >= self._max_attempts:
                        raise StreamError(
                            f"stream ended without a final event after {attempt} attempts"
                        )
                    self._backoff(attempt)
                else:
                    return

            except (httpx.HTTPError, httpx.StreamError) as exc:
                attempt += 1
                if attempt >= self._max_attempts:
                    raise StreamError(
                        f"stream failed after {attempt} attempts: {exc}"
                    ) from exc
                if self._on_reconnect:
                    self._on_reconnect(attempt, self._last_seq)
                self._backoff(attempt)

    def _read_once(self) -> Iterator[Event]:
        # Resume from the cursor. The server replays the durable trace from here
        # and then switches to live, so nothing is missed and nothing repeats.
        params = {"after": self._last_seq} if self._last_seq else None

        with self._client.stream(
            "GET", self._url, params=params, headers=self._headers, timeout=None
        ) as response:
            if response.status_code >= 400:
                body = response.read().decode("utf-8", "replace")
                raise StreamError(f"stream returned {response.status_code}: {body[:200]}")

            for seq, event_type, payload in parse_sse(response.iter_lines()):
                if seq is None:
                    continue
                # Enforced here as well as on the server. One integer comparison
                # against a caller double-counting tokens.
                if seq <= self._last_seq:
                    continue
                self._last_seq = seq

                yield Event(seq=seq, type=event_type, payload=payload)

                if event_type == "final":
                    self._finished = True
                    return

    def _backoff(self, attempt: int) -> None:
        delay = min(INITIAL_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
        time.sleep(delay)
