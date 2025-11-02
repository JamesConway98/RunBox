"""Provider selection.

The interface in `llm.py` was written to be implementable more than once. This
module is the proof: adding OpenAI-compatible models is a class and a lookup,
not a refactor of the agent loop.

Routing is by model id prefix rather than by a `provider` field on the request.
The caller says `gpt-4o-mini`, not `openai/gpt-4o-mini`, because the model id is
already unique and making callers repeat information the platform can derive is
how APIs get tedious.
"""

from __future__ import annotations

import llm
import llm_openai


def _is_anthropic(model: str) -> bool:
    return model.startswith("claude-")


def _is_openai(model: str) -> bool:
    return model.startswith(("gpt-", "o1", "o3", "o4"))


def for_model(model: str) -> llm.Provider:
    """Return a provider that can serve this model.

    Raises LLMError for an unknown model rather than guessing. A wrong guess
    produces a confusing 404 from somebody else's API; a clear error here names
    the actual problem.
    """
    if _is_anthropic(model):
        return llm.AnthropicProvider()
    if _is_openai(model):
        return llm_openai.OpenAIProvider()
    raise llm.LLMError(f"no provider is registered for model {model!r}")
