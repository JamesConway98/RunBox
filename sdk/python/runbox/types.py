"""Typed responses.

Dataclasses rather than dicts. The difference is that `run.usage.cost` is a
typo an editor catches, while `run["usage"]["cost"]` is a KeyError in
production.

Every model is constructed through `from_api`, which ignores fields it does not
know about. That is deliberate: the server adding a field must never break an
older client, and a client pinned to last month's version should keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "timeout"})


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        # Python's fromisoformat did not accept a trailing Z until 3.11, and
        # this package supports 3.9.
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    compute_ms: int = 0
    cost_micros: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost(self) -> float:
        """Cost in dollars.

        The integer micros are the authoritative value and are kept as-is; this
        is a convenience for printing. Never add these floats together — sum
        `cost_micros` and convert once at the end.
        """
        return self.cost_micros / 1_000_000

    @classmethod
    def from_api(cls, data: dict | None) -> Usage:
        if not data:
            return cls()
        return cls(
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            tool_calls=data.get("tool_calls", 0),
            compute_ms=data.get("compute_ms", 0),
            cost_micros=data.get("cost_micros", 0),
        )


@dataclass
class Event:
    """One trace event.

    Tool and token fields are lifted out of the payload onto the object, because
    `event.tool` reads better than `event.payload["tool"]` in the loop this
    exists to make pleasant. The full payload stays available for anything the
    SDK has not lifted.
    """

    seq: int
    type: str
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.payload.get("text", "")

    @property
    def tool(self) -> str:
        return self.payload.get("tool", "")

    @property
    def args(self) -> dict[str, Any]:
        return self.payload.get("args", {})

    @property
    def output(self) -> str:
        return self.payload.get("output", "")

    @property
    def ok(self) -> bool:
        return bool(self.payload.get("ok", True))

    @property
    def message(self) -> str:
        return self.payload.get("message", "")

    @property
    def is_final(self) -> bool:
        return self.type == "final"

    def __repr__(self) -> str:
        detail = {
            "token": lambda: repr(self.text[:40]),
            "tool_call": lambda: f"{self.tool}({self.args})",
            "tool_result": lambda: f"{self.tool} ok={self.ok}",
            "error": lambda: self.message,
        }.get(self.type)
        suffix = f" {detail()}" if detail else ""
        return f"<Event {self.seq} {self.type}{suffix}>"


@dataclass
class RunData:
    """The server's view of a run."""

    id: str
    status: str
    task: str
    model: str
    tools: list[str] = field(default_factory=list)
    result: str | None = None
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    usage: Usage = field(default_factory=Usage)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @classmethod
    def from_api(cls, data: dict) -> RunData:
        # Only known keys are read. An unknown field from a newer server is
        # ignored rather than raising, so an old client keeps working.
        return cls(
            id=data["id"],
            status=data["status"],
            task=data.get("task", ""),
            model=data.get("model", ""),
            tools=list(data.get("tools") or []),
            result=data.get("result"),
            error=data.get("error"),
            created_at=_parse_time(data.get("created_at")),
            started_at=_parse_time(data.get("started_at")),
            finished_at=_parse_time(data.get("finished_at")),
            duration_ms=data.get("duration_ms"),
            usage=Usage.from_api(data.get("usage")),
        )
