"""Caller-supplied provider keys.

Runbox does not hold a model provider key. Callers bring their own, and the
platform's job is to get it to the sandbox's proxy without ever making it the
platform's problem.

The rules that make that claim true, rather than a slogan:

- **Never written to Postgres.** The system of record has no column for it, so
  a database dump, a backup, or a stray `select *` cannot leak one.
- **Redis only, with a TTL.** The key lives under a run-scoped Redis entry that
  expires on its own. Nothing has to remember to clean up for the guarantee to
  hold.
- **Deleted on read.** The runner fetches and deletes in one round trip, so a
  key normally exists for the second or two between enqueue and claim.
- **Never logged.** Only ever referenced by a prefix, and the shape check below
  is the only place the value is inspected at all.

What this does not do is protect the key from the platform *operator*. It
transits this process and sits in Redis briefly, so an operator with production
access could capture it. That is true of every hosted BYOK product and is worth
stating plainly rather than implying otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import HTTPException, status

# How long a key may sit in Redis waiting for a worker. Long enough to survive a
# queue backlog, short enough that an abandoned run does not leave one lying
# around. The runner deletes on read, so this is a backstop, not the mechanism.
TTL_SECONDS = 900

KEY_PREFIX = "runbox:pkey:"

# Shape only. The provider is the authority on whether a key is valid, and
# checking it here would mean a network round trip on every run to learn
# something the first model call establishes anyway. This catches the common
# mistakes — a Runbox key pasted into the provider field, an empty string,
# somebody's whole shell export line.
PATTERNS = {
    "anthropic": re.compile(r"^sk-ant-[A-Za-z0-9\-_]{20,}$"),
    "openai": re.compile(r"^sk-(?:proj-)?[A-Za-z0-9\-_]{20,}$"),
}

MAX_KEY_LENGTH = 300


@dataclass(frozen=True)
class ProviderKey:
    provider: str
    value: str

    @property
    def display(self) -> str:
        """A prefix safe to log or show in an error."""
        return f"{self.value[:11]}…" if len(self.value) > 11 else "…"


def parse(raw: str | None) -> ProviderKey | None:
    """Validate a caller-supplied key, or None when absent."""
    if raw is None:
        return None

    value = raw.strip()
    if not value:
        return None

    if len(value) > MAX_KEY_LENGTH:
        raise _bad("Provider key is implausibly long.")

    if value.startswith("rb_live_"):
        # Easy mistake, and a confusing one to debug from the provider's 401.
        raise _bad(
            "That is a Runbox API key, not a model provider key. "
            "The provider key belongs in X-Provider-Key and starts with 'sk-'."
        )

    for provider, pattern in PATTERNS.items():
        if pattern.match(value):
            return ProviderKey(provider=provider, value=value)

    raise _bad(
        "Provider key is not a recognised format. Expected an Anthropic key "
        "starting 'sk-ant-' or an OpenAI key starting 'sk-'."
    )


def provider_for_model(model: str) -> str:
    """Which provider a model id belongs to.

    Mirrors the agent's routing so a mismatch is caught at the edge, before a
    container exists, rather than as a 401 from somebody else's API halfway
    through a run.
    """
    if model.startswith("claude-"):
        return "anthropic"
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    return "unknown"


def require_match(key: ProviderKey, model: str) -> None:
    expected = provider_for_model(model)
    if expected != "unknown" and key.provider != expected:
        raise _bad(
            f"Model '{model}' needs a {expected} key, but the supplied key "
            f"looks like {key.provider}."
        )


async def store(redis, run_id: str, key: ProviderKey) -> None:
    """Park the key for the runner to collect.

    `set` with `ex` rather than a separate `expire`: one round trip, and no
    window in which a key exists without a TTL because the second call failed.
    """
    await redis.set(f"{KEY_PREFIX}{run_id}", key.value, ex=TTL_SECONDS)


async def discard(redis, run_id: str) -> None:
    """Drop a key for a run that will never execute."""
    await redis.delete(f"{KEY_PREFIX}{run_id}")


def _bad(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "invalid_provider_key", "message": message},
    )
