"""Trace emission.

The agent's entire contract with the outside world is one JSON object per line
on stdout. No sequence numbers here — the runner assigns those, because it is
the only component that can guarantee monotonicity if the agent is ever
restarted or its output interleaved.

Everything is flushed immediately. A buffered trace is a trace that arrives
after the container has already been killed.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any


def _now_ms() -> int:
    return int(time.time() * 1000)


class Trace:
    """Writes newline-delimited trace events to a stream (stdout by default)."""

    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def emit(self, type_: str, **payload: Any) -> None:
        record = {"type": type_, "ts": _now_ms(), **payload}
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            # Never let an unserialisable payload take down the run. Losing one
            # event's detail is strictly better than losing the run.
            line = json.dumps(
                {"type": "error", "ts": _now_ms(), "message": f"unserialisable {type_} payload"}
            )
        self._stream.write(line + "\n")
        self._stream.flush()

    # Convenience wrappers. These exist so the event vocabulary is defined in
    # exactly one place rather than scattered as string literals.

    def llm_call(self, model: str, messages: int, tools: list[str]) -> None:
        self.emit("llm_call", model=model, messages=messages, tools=tools)

    def token(self, text: str) -> None:
        self.emit("token", text=text)

    def tool_call(self, tool: str, args: dict, call_id: str) -> None:
        self.emit("tool_call", tool=tool, args=args, call_id=call_id)

    def tool_result(self, tool: str, call_id: str, ok: bool, output: str, ms: int) -> None:
        self.emit(
            "tool_result", tool=tool, call_id=call_id, ok=ok, output=output, duration_ms=ms
        )

    def error(self, message: str, retryable: bool = False) -> None:
        self.emit("error", message=message, retryable=retryable)

    def final(self, status: str, result: str | None, usage: dict) -> None:
        self.emit("final", status=status, result=result, usage=usage)
