"""The same prompt across several models, streaming concurrently.

Threads rather than asyncio: the sync client is what most people will reach
for, and a thread per model is both simpler to read and entirely adequate for a
handful of concurrent streams.

    python examples/compare_models.py "Explain CAP theorem in two sentences"
"""

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from runbox import Runbox, RunboxError

MODELS = ["claude-haiku-4-5", "claude-sonnet-5", "gpt-4o-mini"]


@dataclass
class Result:
    model: str
    text: str
    tokens: int
    cost: float
    ms: int | None
    error: str | None = None


def run_one(rb: Runbox, model: str, task: str) -> Result:
    try:
        run = rb.runs.create(task=task, model=model, max_tokens=1024)
        # Consume the stream so first-token latency is real work, then read the
        # refreshed run for the authoritative usage numbers.
        text = "".join(e.text for e in run.stream() if e.type == "token")
        return Result(model, text.strip(), run.usage.total_tokens, run.usage.cost, run.duration_ms)
    except RunboxError as exc:
        return Result(model, "", 0, 0.0, None, error=str(exc))


def main() -> int:
    task = " ".join(sys.argv[1:]) or "Explain the CAP theorem in two sentences."

    # One thread per model. They start together and finish whenever they
    # finish; nothing here waits on anything else.
    with Runbox() as rb, ThreadPoolExecutor(max_workers=len(MODELS)) as pool:
        results = list(pool.map(lambda m: run_one(rb, m, task), MODELS))

    for result in results:
        print(f"\n{'=' * 70}\n{result.model}\n{'=' * 70}")
        if result.error:
            print(f"failed: {result.error}")
            continue
        print(result.text)
        print(f"\n  {result.tokens} tokens · ${result.cost:.4f} · {result.ms}ms")

    ranked = sorted((r for r in results if not r.error), key=lambda r: r.cost)
    if ranked:
        print(f"\ncheapest: {ranked[0].model} at ${ranked[0].cost:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
