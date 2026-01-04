"""Runbox — sandboxed execution and observability for LLM agents.

    from runbox import Runbox

    rb = Runbox(api_key="rb_live_...")
    run = rb.runs.create(task="Summarise the last three Go releases", tools=["http_get"])

    for event in run.stream():
        if event.type == "tool_call":
            print(f"→ {event.tool}({event.args})")
        elif event.type == "token":
            print(event.text, end="", flush=True)

    print(run.result)
    print(f"{run.usage.total_tokens} tokens, ${run.usage.cost:.4f}")
"""

from .client import Run, Runbox
from .errors import (
    APIError,
    AuthenticationError,
    ConnectionError,
    InvalidRequestError,
    NotFoundError,
    RateLimitError,
    RunboxError,
    ServerError,
    StreamError,
    TimeoutError,
)
from .types import Event, RunData, Usage

__version__ = "0.1.0"

__all__ = [
    "Runbox",
    "Run",
    "RunData",
    "Event",
    "Usage",
    "RunboxError",
    "APIError",
    "AuthenticationError",
    "NotFoundError",
    "RateLimitError",
    "InvalidRequestError",
    "ServerError",
    "ConnectionError",
    "StreamError",
    "TimeoutError",
]
