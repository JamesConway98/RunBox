"""The agent loop.

Runs as PID 1 inside the sandbox container. Reads its task from the
environment, writes newline-delimited trace events to stdout, and exits 0 on a
completed run or non-zero on a failure it could not turn into a trace event.

The loop itself is the boring part, and that is the point: call the model, run
any tools it asked for, feed the results back, repeat until it stops asking for
tools or we hit a ceiling.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from typing import Any

import llm
import tools as toolkit
from trace import Trace

DEFAULT_SYSTEM = """You are an agent running inside a sandboxed container.

You have a small set of tools. Use them when they help and answer directly when \
they do not. You have no network access except through the tools provided.

When you have the answer, state it plainly. Do not narrate your process."""

# A hard ceiling on loop iterations, independent of the token budget. A model
# that gets stuck calling the same tool forever should terminate on its own
# rather than waiting for the runner's wall-clock kill.
MAX_ITERATIONS = 24


class Cancelled(Exception):
    pass


def _install_signal_handlers() -> None:
    def handler(signum, _frame):
        raise Cancelled(f"received signal {signum}")

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class Agent:
    def __init__(
        self,
        *,
        provider: llm.Provider,
        trace: Trace,
        model: str,
        tool_names: list[str],
        system: str | None,
        max_tokens: int,
        temperature: float | None,
    ) -> None:
        self._provider = provider
        self._trace = trace
        self._model = model
        self._tools = toolkit.resolve(tool_names)
        self._system = system or DEFAULT_SYSTEM
        self._max_tokens = max_tokens
        self._temperature = temperature

        self._specs = [
            llm.ToolSpec(name=t.name, description=t.description, input_schema=t.input_schema)
            for t in self._tools
        ]
        self._by_name = {t.name: t for t in self._tools}

        self.input_tokens = 0
        self.output_tokens = 0
        self.tool_calls = 0

    @property
    def usage(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tool_calls": self.tool_calls,
        }

    def run(self, task: str) -> str:
        messages: list[llm.Message] = [
            llm.Message(role="user", content=[{"type": "text", "text": task}])
        ]

        for iteration in range(1, MAX_ITERATIONS + 1):
            completed = self._one_turn(messages)

            if completed.stop_reason != "tool_use" or not completed.tool_calls:
                return completed.text.strip()

            # Record the assistant turn verbatim, including the tool_use blocks,
            # so the provider sees a well-formed conversation on the next call.
            assistant_blocks: list[dict[str, Any]] = []
            if completed.text:
                assistant_blocks.append({"type": "text", "text": completed.text})
            for call in completed.tool_calls:
                assistant_blocks.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": call.args}
                )
            messages.append(llm.Message(role="assistant", content=assistant_blocks))

            results = [self._execute(call) for call in completed.tool_calls]
            messages.append(llm.Message(role="user", content=results))

            if self._budget_exhausted():
                self._trace.error("token budget exhausted", retryable=False)
                return completed.text.strip() or "(stopped: token budget exhausted)"

        self._trace.error(f"stopped after {MAX_ITERATIONS} iterations", retryable=False)
        return "(stopped: iteration limit reached)"

    def _one_turn(self, messages: list[llm.Message]) -> llm.Completed:
        self._trace.llm_call(
            model=self._model, messages=len(messages), tools=[t.name for t in self._tools]
        )

        def call() -> llm.Completed:
            completed: llm.Completed | None = None
            for event in self._provider.stream(
                model=self._model,
                system=self._system,
                messages=messages,
                tools=self._specs,
                max_tokens=self._remaining_tokens(),
                temperature=self._temperature,
            ):
                if isinstance(event, llm.TextDelta):
                    self._trace.token(event.text)
                elif isinstance(event, llm.ToolCallComplete):
                    self._trace.tool_call(
                        tool=event.call.name, args=event.call.args, call_id=event.call.id
                    )
                elif isinstance(event, llm.Completed):
                    completed = event
            if completed is None:
                raise llm.LLMError("provider stream ended without completing", retryable=True)
            return completed

        def on_retry(exc: llm.LLMError, attempt: int, delay: float) -> None:
            self._trace.error(
                f"provider error (attempt {attempt}), retrying in {delay:.1f}s: {exc}",
                retryable=True,
            )

        completed = llm.with_retries(call, on_retry=on_retry)
        self.input_tokens += completed.usage.input_tokens
        self.output_tokens += completed.usage.output_tokens
        return completed

    def _execute(self, call: llm.ToolCall) -> dict[str, Any]:
        self.tool_calls += 1
        tool = self._by_name.get(call.name)
        started = time.monotonic()

        if tool is None:
            message = f"unknown tool: {call.name}"
            self._trace.tool_result(call.name, call.id, False, message, 0)
            return self._tool_result_block(call.id, message, is_error=True)

        try:
            output = toolkit.invoke(tool, call.args)
            ok, text = True, output
        except toolkit.ToolError as exc:
            ok, text = False, str(exc)

        elapsed = int((time.monotonic() - started) * 1000)
        self._trace.tool_result(call.name, call.id, ok, text, elapsed)
        return self._tool_result_block(call.id, text, is_error=not ok)

    @staticmethod
    def _tool_result_block(call_id: str, content: str, *, is_error: bool) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": call_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True
        return block

    def _remaining_tokens(self) -> int:
        # Leave enough headroom that the final turn can actually say something.
        spent = self.output_tokens
        return max(512, min(8192, self._max_tokens - spent))

    def _budget_exhausted(self) -> bool:
        return (self.input_tokens + self.output_tokens) >= self._max_tokens


def main() -> int:
    _install_signal_handlers()
    trace = Trace()

    task = os.environ.get("RUNBOX_TASK", "").strip()
    if not task:
        trace.error("RUNBOX_TASK is empty")
        trace.final("failed", None, {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0})
        return 2

    model = os.environ.get("RUNBOX_MODEL", "claude-sonnet-5")
    tool_names = [t for t in os.environ.get("RUNBOX_TOOLS", "").split(",") if t.strip()]
    system = os.environ.get("RUNBOX_SYSTEM_PROMPT") or None
    max_tokens = _env_int("RUNBOX_MAX_TOKENS", 20_000)
    temperature = _env_float("RUNBOX_TEMPERATURE")

    agent: Agent | None = None
    try:
        provider = llm.AnthropicProvider()
        agent = Agent(
            provider=provider,
            trace=trace,
            model=model,
            tool_names=tool_names,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        result = agent.run(task)
    except Cancelled as exc:
        trace.error(str(exc))
        trace.final("cancelled", None, agent.usage if agent else _zero_usage())
        return 0
    except toolkit.ToolError as exc:
        trace.error(f"tool configuration error: {exc}")
        trace.final("failed", None, agent.usage if agent else _zero_usage())
        return 0
    except llm.LLMError as exc:
        trace.error(str(exc), retryable=exc.retryable)
        trace.final("failed", None, agent.usage if agent else _zero_usage())
        return 0
    except Exception as exc:  # noqa: BLE001 — always exit through a final event
        trace.error(f"unhandled: {type(exc).__name__}: {exc}")
        trace.final("failed", None, agent.usage if agent else _zero_usage())
        return 1

    trace.final("succeeded", result, agent.usage)
    return 0


def _zero_usage() -> dict[str, int]:
    return {"input_tokens": 0, "output_tokens": 0, "tool_calls": 0}


if __name__ == "__main__":
    sys.exit(main())
