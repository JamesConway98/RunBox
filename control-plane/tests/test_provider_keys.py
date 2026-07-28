from __future__ import annotations

import pytest
from fastapi import HTTPException

from runbox_api import provider_keys

ANTHROPIC = "sk-ant-api03-" + "A" * 40
OPENAI = "sk-proj-" + "B" * 40


class TestParse:
    def test_anthropic_key(self):
        key = provider_keys.parse(ANTHROPIC)
        assert key is not None
        assert key.provider == "anthropic"

    def test_openai_key(self):
        key = provider_keys.parse(OPENAI)
        assert key is not None
        assert key.provider == "openai"

    def test_absent_is_none_not_an_error(self):
        # The endpoint decides whether a key is required; parsing does not.
        assert provider_keys.parse(None) is None
        assert provider_keys.parse("") is None
        assert provider_keys.parse("   ") is None

    def test_surrounding_whitespace_is_tolerated(self):
        # Pasting from a terminal picks up a trailing newline more often than not.
        assert provider_keys.parse(f"  {ANTHROPIC}\n") is not None

    def test_runbox_key_gets_a_specific_message(self):
        # The most likely mistake, and one that would otherwise surface as an
        # opaque 401 from the provider halfway through a run.
        with pytest.raises(HTTPException) as exc:
            provider_keys.parse("rb_live_Ezxg36QYsNfrgcT53K4hYZwtRxnddjnx")
        message = exc.value.detail["message"]
        assert "Runbox API key" in message
        assert "X-Provider-Key" in message

    @pytest.mark.parametrize(
        "value",
        [
            "not-a-key",
            "sk-",  # prefix only
            "sk-ant-short",  # too short to be real
            "Bearer sk-ant-api03-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",  # whole header pasted
        ],
    )
    def test_malformed_keys_are_rejected(self, value):
        with pytest.raises(HTTPException) as exc:
            provider_keys.parse(value)
        assert exc.value.status_code == 400

    def test_implausibly_long_input_is_rejected(self):
        with pytest.raises(HTTPException, match="long"):
            provider_keys.parse("sk-ant-" + "A" * 500)


class TestProviderMatching:
    @pytest.mark.parametrize(
        ("model", "provider"),
        [
            ("claude-sonnet-5", "anthropic"),
            ("claude-haiku-4-5", "anthropic"),
            ("gpt-4o", "openai"),
            ("gpt-4o-mini", "openai"),
            ("o3-mini", "openai"),
            ("llama-3", "unknown"),
        ],
    )
    def test_provider_for_model(self, model, provider):
        assert provider_keys.provider_for_model(model) == provider

    def test_matching_key_and_model_is_accepted(self):
        provider_keys.require_match(provider_keys.parse(ANTHROPIC), "claude-sonnet-5")
        provider_keys.require_match(provider_keys.parse(OPENAI), "gpt-4o")

    def test_mismatch_is_caught_at_the_edge(self):
        # Better here than as a 401 from the provider after a container exists.
        with pytest.raises(HTTPException) as exc:
            provider_keys.require_match(provider_keys.parse(OPENAI), "claude-sonnet-5")
        assert "anthropic" in exc.value.detail["message"]

    def test_unknown_model_is_not_second_guessed(self):
        # The model itself is validated against model_pricing elsewhere; this
        # check should not also become a model allowlist.
        provider_keys.require_match(provider_keys.parse(ANTHROPIC), "some-future-model")


def test_display_never_reveals_the_whole_key():
    key = provider_keys.parse(ANTHROPIC)
    assert key.value not in key.display
    assert len(key.display) < 20
