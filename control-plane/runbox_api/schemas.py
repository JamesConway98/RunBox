from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled", "timeout"]
EventType = Literal[
    "llm_call", "token", "tool_call", "tool_result", "usage", "error", "final"
]

TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled", "timeout"})


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=20_000)
    model: str = Field(default="claude-sonnet-5", max_length=100)
    tools: list[str] = Field(default_factory=list, max_length=16)
    system_prompt: str | None = Field(default=None, max_length=10_000)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    timeout_s: int = Field(default=120, ge=1, le=600)
    max_tokens: int = Field(default=20_000, ge=256, le=200_000)

    @field_validator("task")
    @classmethod
    def task_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("task cannot be blank")
        return v

    @field_validator("tools")
    @classmethod
    def tools_unique(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("tools must be unique")
        return v


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    compute_ms: int = 0
    cost_micros: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class Run(BaseModel):
    id: str
    status: RunStatus
    task: str
    model: str
    tools: list[str] = Field(default_factory=list)
    result: str | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None
    usage: Usage | None = None


class RunCreated(BaseModel):
    id: str
    status: RunStatus


class TraceEvent(BaseModel):
    seq: int
    type: EventType
    payload: dict[str, Any]
    created_at: datetime


class Page(BaseModel):
    """Cursor pagination.

    An opaque cursor rather than an offset: offsets shift under you when rows
    are inserted, which for a live-updating runs list is not hypothetical.
    """

    has_more: bool = False
    next_cursor: str | None = None


class RunList(Page):
    data: list[Run]


class EventList(Page):
    data: list[TraceEvent]


class ErrorResponse(BaseModel):
    error: str
    message: str
    detail: dict[str, Any] | None = None
