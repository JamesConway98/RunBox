"""OpenAI-compatible provider.

Speaks the Chat Completions shape, which is also what Together, Groq, Fireworks
and most self-hosted gateways speak — so this one file covers a lot more than
one vendor.

The interesting work is translation. The agent loop above this module thinks in
Anthropic-shaped content blocks, because that is what the first implementation
established. Rather than making every caller handle two shapes, this class
converts on the way in and on the way out. The seam is here, which is the whole
argument for having an interface at all.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterator

import httpx

from llm import (
    Completed,
    LLMError,
    Message,
    TextDelta,
    ToolCall,
    ToolCallComplete,
    ToolSpec,
    Usage,
    StreamEvent,
)

DEFAULT_BASE_URL = "https://api.openai.com"


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self._api_key:
            raise LLMError("OPENAI_API_KEY is not set")
        self._base_url = (
            base_url or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")

        # Same unix-socket story as the Anthropic provider: the container has no
        # network, and the runner's proxy holds the real key.
        socket_path = os.environ.get("RUNBOX_LLM_SOCKET", "")
        transport = httpx.HTTPTransport(uds=socket_path) if socket_path else None
        self._client = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=10.0), transport=transport
        )

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
            # Without this the final chunk carries no usage and the run cannot
            # be billed accurately.
            "stream_options": {"include_usage": True},
            "messages": _to_openai_messages(system, messages),
        }
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }

        with self._client.stream(
            "POST", f"{self._base_url}/v1/chat/completions", json=body, headers=headers
        ) as response:
            if response.status_code >= 400:
                detail = response.read().decode("utf-8", "replace")[:500]
                raise LLMError(
                    f"provider returned {response.status_code}: {detail}",
                    retryable=response.status_code in (408, 429, 500, 502, 503, 504),
                    status=response.status_code,
                )
            yield from self._parse_stream(response.iter_lines())

    def _parse_stream(self, lines: Iterator[str]) -> Iterator[StreamEvent]:
        text_parts: list[str] = []
        usage = Usage()
        finish_reason = "stop"

        # Tool calls stream as fragments keyed by index, with the name arriving
        # once and the arguments dribbling in across many chunks.
        partial: dict[int, dict[str, str]] = {}

        for raw in lines:
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except ValueError:
                continue

            if chunk_usage := chunk.get("usage"):
                usage.input_tokens = chunk_usage.get("prompt_tokens", usage.input_tokens)
                usage.output_tokens = chunk_usage.get("completion_tokens", usage.output_tokens)

            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            finish_reason = choice.get("finish_reason") or finish_reason
            delta = choice.get("delta") or {}

            if content := delta.get("content"):
                text_parts.append(content)
                yield TextDelta(content)

            for fragment in delta.get("tool_calls") or []:
                index = fragment.get("index", 0)
                slot = partial.setdefault(index, {"id": "", "name": "", "args": ""})
                if call_id := fragment.get("id"):
                    slot["id"] = call_id
                function = fragment.get("function") or {}
                if name := function.get("name"):
                    slot["name"] = name
                if args := function.get("arguments"):
                    slot["args"] += args

        # Unlike Anthropic's stream there is no per-block stop event, so the
        # calls can only be finalised once the stream ends.
        tool_calls: list[ToolCall] = []
        for _, slot in sorted(partial.items()):
            if not slot["name"]:
                continue
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except ValueError:
                args = {}
            call = ToolCall(id=slot["id"] or f"call_{len(tool_calls)}", name=slot["name"], args=args)
            tool_calls.append(call)
            yield ToolCallComplete(call)

        yield Completed(
            # Normalised to the vocabulary the agent loop already understands,
            # so the loop does not need to know which provider it is talking to.
            stop_reason="tool_use" if tool_calls else _normalise_stop(finish_reason),
            usage=usage,
            text="".join(text_parts),
            tool_calls=tool_calls,
        )


def _normalise_stop(reason: str) -> str:
    return {"length": "max_tokens", "tool_calls": "tool_use"}.get(reason, "end_turn")


def _to_openai_messages(system: str | None, messages: list[Message]) -> list[dict[str, Any]]:
    """Translate Anthropic-shaped content blocks into Chat Completions turns.

    The two shapes disagree in one structural way that matters: Anthropic puts
    tool results in a user turn as blocks, while OpenAI wants a separate message
    per result with role "tool". So one input message can become several.
    """
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for message in messages:
        if message.role == "assistant":
            text = "".join(b.get("text", "") for b in message.content if b.get("type") == "text")
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))},
                }
                for b in message.content
                if b.get("type") == "tool_use"
            ]
            turn: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                turn["tool_calls"] = calls
            out.append(turn)
            continue

        # User turn: split tool results out into their own messages.
        results = [b for b in message.content if b.get("type") == "tool_result"]
        text_blocks = [b for b in message.content if b.get("type") == "text"]

        for block in results:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": str(block.get("content", "")),
                }
            )
        if text_blocks:
            out.append(
                {"role": "user", "content": "".join(b.get("text", "") for b in text_blocks)}
            )

    return out
