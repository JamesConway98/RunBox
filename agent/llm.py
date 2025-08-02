"""Provider interface and the Anthropic implementation.

Deliberately thin. Runbox is not multi-model as a feature — it is multi-model
behind an interface, so that adding a provider is a file rather than a
refactor. Everything above this module speaks in `Message`, `ToolSpec` and
`StreamEvent` and never sees a provider's wire format.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, Union

import httpx

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_BASE_URL = "https://api.anthropic.com"


class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Message:
    """A provider-neutral turn. `content` is a list of blocks."""

    role: str  # "user" | "assistant"
    content: list[dict]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


# --- stream events -----------------------------------------------------------


@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallComplete:
    call: ToolCall


@dataclass
class Completed:
    stop_reason: str
    usage: Usage
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)


StreamEvent = Union[TextDelta, ToolCallComplete, Completed]


class Provider(Protocol):
    name: str

    def stream(
        self,
        *,
        model: str,
        system: str | None,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        temperature: float | None,
    ) -> Iterator[StreamEvent]: ...


# --- anthropic ---------------------------------------------------------------


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not self._api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._client = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))

    def close(self) -> None:
        self._client.close()

    def stream(
        self,
        *,
        model: str,
        system: str | None,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        temperature: float | None,
    ) -> Iterator[StreamEvent]:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
        }
        if system:
            body["system"] = system
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in tools
            ]

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "accept": "text/event-stream",
        }

        with self._client.stream(
            "POST", f"{self._base_url}/v1/messages", json=body, headers=headers
        ) as response:
            if response.status_code >= 400:
                detail = response.read().decode("utf-8", "replace")[:500]
                raise LLMError(
                    f"provider returned {response.status_code}: {detail}",
                    retryable=response.status_code in (408, 429, 500, 502, 503, 504),
                    status=response.status_code,
                )
            yield from self._parse_stream(response.iter_lines())

    # The SSE shape is stable enough to parse by hand, and doing so keeps the
    # container image small and the failure modes visible.
    def _parse_stream(self, lines: Iterator[str]) -> Iterator[StreamEvent]:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage = Usage()
        stop_reason = "end_turn"

        # Partial tool_use blocks, keyed by content-block index.
        pending: dict[int, dict] = {}

        for raw in lines:
            line = raw.strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
            except ValueError:
                continue

            kind = event.get("type")

            if kind == "message_start":
                start_usage = event.get("message", {}).get("usage", {})
                usage.input_tokens = start_usage.get("input_tokens", 0)

            elif kind == "content_block_start":
                block = event.get("content_block", {})
                if block.get("type") == "tool_use":
                    pending[event["index"]] = {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "json": "",
                    }

            elif kind == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    chunk = delta.get("text", "")
                    if chunk:
                        text_parts.append(chunk)
                        yield TextDelta(chunk)
                elif delta.get("type") == "input_json_delta":
                    slot = pending.get(event["index"])
                    if slot is not None:
                        slot["json"] += delta.get("partial_json", "")

            elif kind == "content_block_stop":
                slot = pending.pop(event.get("index"), None)
                if slot is not None:
                    try:
                        args = json.loads(slot["json"]) if slot["json"].strip() else {}
                    except ValueError:
                        args = {}
                    call = ToolCall(id=slot["id"], name=slot["name"], args=args)
                    tool_calls.append(call)
                    yield ToolCallComplete(call)

            elif kind == "message_delta":
                stop_reason = event.get("delta", {}).get("stop_reason") or stop_reason
                usage.output_tokens = event.get("usage", {}).get(
                    "output_tokens", usage.output_tokens
                )

            elif kind == "error":
                message = event.get("error", {}).get("message", "provider stream error")
                raise LLMError(message, retryable=True)

        yield Completed(
            stop_reason=stop_reason,
            usage=usage,
            text="".join(text_parts),
            tool_calls=tool_calls,
        )


def with_retries(
    fn,
    *,
    attempts: int = 4,
    base_delay: float = 0.75,
    on_retry=None,
):
    """Bounded exponential backoff with jitter around a provider call.

    Only retries errors the provider marked retryable — a 429 or a 5xx. A 400 is
    a bug in our request and retrying it just burns the run's clock.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except LLMError as exc:
            if not exc.retryable or attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.25)
            if on_retry:
                on_retry(exc, attempt, delay)
            time.sleep(delay)
        except httpx.HTTPError as exc:
            if attempt == attempts:
                raise LLMError(f"transport error: {exc}", retryable=True)
            delay = base_delay * (2 ** (attempt - 1))
            if on_retry:
                on_retry(LLMError(str(exc), retryable=True), attempt, delay)
            time.sleep(delay)
    raise LLMError("exhausted retries")
