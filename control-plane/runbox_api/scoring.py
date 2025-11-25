"""Scorers.

Deterministic scorers are pure functions of (output, expected, config). No IO,
no state, no clock. That makes them trivially testable and, more importantly,
means re-scoring the same run twice always gives the same answer — which is the
whole basis for comparing two models on the same input.

`llm_judge` is the exception and is handled elsewhere: it is implemented as a
Runbox run, so the judge is traced, metered and cancellable like any other.
Building a separate LLM path for judging would mean a second thing to secure.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Compiling arbitrary user regex is a real denial-of-service surface — a
# pathological pattern can hang a worker for minutes. This bound is crude but
# it is the difference between a slow request and a stuck process.
MAX_PATTERN_LENGTH = 200


@dataclass(frozen=True)
class Score:
    passed: bool
    score: float  # 0..1, so scorers average into a single number
    detail: str


class ScorerError(ValueError):
    pass


def _normalise(text: str) -> str:
    """Casefold, collapse whitespace, strip punctuation-ish edges.

    Unicode normalisation matters more than it looks: a model that answers
    "café" with a combining accent is not wrong because the expected value used
    a precomposed é.
    """
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


def exact_match(output: str, expected: str | None, config: dict[str, Any]) -> Score:
    if expected is None:
        return Score(False, 0.0, "no expected value on this case")

    if config.get("strict"):
        passed = output == expected
        detail = "byte-identical" if passed else "differs"
    else:
        passed = _normalise(output) == _normalise(expected)
        detail = "matches after normalisation" if passed else "differs after normalisation"

    return Score(passed, 1.0 if passed else 0.0, detail)


def contains(output: str, expected: str | None, config: dict[str, Any]) -> Score:
    needle = config.get("value") or expected
    if not needle:
        return Score(False, 0.0, "no value to look for")

    haystack = output if config.get("case_sensitive") else _normalise(output)
    target = needle if config.get("case_sensitive") else _normalise(needle)

    passed = target in haystack
    return Score(
        passed,
        1.0 if passed else 0.0,
        f"{'found' if passed else 'did not find'} {_ellipsis(needle)}",
    )


def regex(output: str, expected: str | None, config: dict[str, Any]) -> Score:
    pattern = config.get("pattern") or expected
    if not pattern:
        return Score(False, 0.0, "no pattern configured")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ScorerError(f"Pattern is longer than {MAX_PATTERN_LENGTH} characters.")

    flags = 0 if config.get("case_sensitive") else re.IGNORECASE
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise ScorerError(f"Invalid regex: {exc}") from exc

    match = compiled.search(output)
    return Score(
        bool(match),
        1.0 if match else 0.0,
        f"matched {_ellipsis(match.group(0))}" if match else "no match",
    )


def latency(output: str, expected: str | None, config: dict[str, Any]) -> Score:
    """Pass when the run finished inside a threshold.

    Scored on a ramp rather than a cliff. A run 1ms over the threshold is not
    meaningfully worse than one 1ms under, and a binary score would rank them as
    though it were.
    """
    threshold = config.get("threshold_ms")
    actual = config.get("duration_ms")

    if threshold is None:
        raise ScorerError("latency scorer requires threshold_ms")
    if actual is None:
        return Score(False, 0.0, "run has no recorded duration")

    passed = actual <= threshold
    # Full marks at or under the threshold, decaying to zero at 2x.
    ratio = actual / threshold if threshold else float("inf")
    score = 1.0 if passed else max(0.0, 2.0 - ratio)

    return Score(passed, round(score, 4), f"{actual}ms against a {threshold}ms threshold")


Scorer = Callable[[str, "str | None", dict[str, Any]], Score]

DETERMINISTIC: dict[str, Scorer] = {
    "exact_match": exact_match,
    "contains": contains,
    "regex": regex,
    "latency": latency,
}

# Handled as a run rather than inline. Listed here so the API can validate a
# scorer name without pretending it is callable.
LLM_JUDGE = "llm_judge"

ALL_SCORERS = (*DETERMINISTIC.keys(), LLM_JUDGE)

JUDGE_PROMPT = """You are grading one model output against a reference answer.

Question:
{input}

Reference answer:
{expected}

Model output:
{output}

Reply with exactly one line: PASS or FAIL, then a dash and a short reason.
Grade on whether the output is substantively correct, not on wording."""


def parse_judge_verdict(text: str) -> Score:
    """Read a judge run's final answer.

    Deliberately forgiving about surrounding prose and deliberately strict about
    the verdict: an unparseable judgement is a failure to grade, not a pass. The
    alternative — defaulting to pass — would quietly inflate every pass rate.
    """
    stripped = text.strip()
    match = re.search(r"\b(PASS|FAIL)\b", stripped, re.IGNORECASE)
    if not match:
        return Score(False, 0.0, "judge did not return a verdict")

    passed = match.group(1).upper() == "PASS"
    _, _, reason = stripped.partition(match.group(0))
    detail = reason.lstrip(" -–—:").strip() or ("judged correct" if passed else "judged incorrect")

    return Score(passed, 1.0 if passed else 0.0, _ellipsis(detail, 200))


def score(name: str, output: str, expected: str | None, config: dict[str, Any]) -> Score:
    scorer = DETERMINISTIC.get(name)
    if scorer is None:
        raise ScorerError(f"Unknown scorer: {name}")
    return scorer(output or "", expected, config)


def _ellipsis(text: str, limit: int = 60) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[:limit]}…"
