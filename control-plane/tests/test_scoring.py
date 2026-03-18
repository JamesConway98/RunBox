from __future__ import annotations

import pytest

from runbox_api import scoring


class TestExactMatch:
    def test_identical(self):
        assert scoring.score("exact_match", "Paris", "Paris", {}).passed

    def test_normalises_case_and_whitespace_by_default(self):
        assert scoring.score("exact_match", "  paris\n", "Paris", {}).passed

    def test_strict_mode_does_not_normalise(self):
        assert not scoring.score("exact_match", " paris ", "Paris", {"strict": True}).passed

    def test_unicode_normalisation(self):
        # "café" precomposed vs. with a combining accent. A model is not wrong
        # because it chose a different encoding of the same string.
        precomposed = "café"
        combining = "café"
        assert scoring.score("exact_match", combining, precomposed, {}).passed

    def test_missing_expected_fails_rather_than_passing_vacuously(self):
        result = scoring.score("exact_match", "anything", None, {})
        assert not result.passed
        assert "no expected value" in result.detail


class TestContains:
    def test_finds_substring(self):
        assert scoring.score("contains", "The capital is Paris.", "Paris", {}).passed

    def test_case_insensitive_by_default(self):
        assert scoring.score("contains", "the capital is paris", "Paris", {}).passed

    def test_case_sensitive_when_configured(self):
        result = scoring.score(
            "contains", "the capital is paris", "Paris", {"case_sensitive": True}
        )
        assert not result.passed

    def test_explicit_value_overrides_expected(self):
        assert scoring.score("contains", "answer: 42", "Paris", {"value": "42"}).passed


class TestRegex:
    def test_matches(self):
        result = scoring.score("regex", "the answer is 42", None, {"pattern": r"\d+"})
        assert result.passed
        assert "42" in result.detail

    def test_no_match(self):
        assert not scoring.score("regex", "no digits", None, {"pattern": r"\d+"}).passed

    def test_invalid_pattern_is_a_scorer_error_not_a_crash(self):
        with pytest.raises(scoring.ScorerError, match="Invalid regex"):
            scoring.score("regex", "x", None, {"pattern": "([unclosed"})

    def test_overlong_pattern_is_refused(self):
        # A pathological pattern can hang a worker for minutes.
        with pytest.raises(scoring.ScorerError, match="longer than"):
            scoring.score("regex", "x", None, {"pattern": "a" * 500})


class TestLatency:
    def test_under_threshold_passes_with_full_score(self):
        result = scoring.score("latency", "", None, {"threshold_ms": 5000, "duration_ms": 1200})
        assert result.passed
        assert result.score == 1.0

    def test_just_over_threshold_scores_near_one_not_zero(self):
        # A cliff would rank a run 1ms over as equal to one twice as slow.
        result = scoring.score("latency", "", None, {"threshold_ms": 1000, "duration_ms": 1001})
        assert not result.passed
        assert 0.99 < result.score < 1.0

    def test_score_decays_to_zero_at_double_the_threshold(self):
        result = scoring.score("latency", "", None, {"threshold_ms": 1000, "duration_ms": 2000})
        assert result.score == 0.0

    def test_never_goes_negative(self):
        result = scoring.score("latency", "", None, {"threshold_ms": 1000, "duration_ms": 60_000})
        assert result.score == 0.0

    def test_missing_threshold_is_a_configuration_error(self):
        with pytest.raises(scoring.ScorerError, match="threshold_ms"):
            scoring.score("latency", "", None, {"duration_ms": 100})


class TestJudgeVerdict:
    def test_pass_with_reason(self):
        result = scoring.parse_judge_verdict("PASS - correctly identifies Paris")
        assert result.passed
        assert result.detail == "correctly identifies Paris"

    def test_fail_with_reason(self):
        result = scoring.parse_judge_verdict("FAIL — names the wrong city")
        assert not result.passed
        assert "wrong city" in result.detail

    def test_tolerates_surrounding_prose(self):
        assert scoring.parse_judge_verdict("Looking at this, I would say PASS.").passed

    def test_unparseable_verdict_fails_rather_than_passing(self):
        # Defaulting to pass would quietly inflate every pass rate in the
        # product, which is the worst possible direction to be wrong in.
        result = scoring.parse_judge_verdict("I am not sure about this one.")
        assert not result.passed
        assert "did not return a verdict" in result.detail

    def test_empty_response_fails(self):
        assert not scoring.parse_judge_verdict("").passed


def test_unknown_scorer_is_rejected():
    with pytest.raises(scoring.ScorerError, match="Unknown scorer"):
        scoring.score("vibes", "x", "y", {})


def test_scorers_are_deterministic():
    # Re-scoring the same run must give the same answer; comparing two models on
    # the same input depends on it entirely.
    args = ("contains", "The capital is Paris.", "Paris", {})
    first = scoring.score(*args)
    for _ in range(20):
        assert scoring.score(*args) == first
