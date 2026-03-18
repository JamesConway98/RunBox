"""Create a run, wait for it, print the answer.

python examples/basic.py "What is 17 * 23?"
"""

import sys

from runbox import Runbox, RunboxError


def main() -> int:
    task = " ".join(sys.argv[1:]) or "What is 17 * 23? Answer with just the number."

    with Runbox() as rb:  # reads RUNBOX_API_KEY
        run = rb.runs.create(task=task, model="claude-haiku-4-5")
        print(f"run {run.id} queued…")

        run.wait(timeout=120)

        if not run.data.succeeded:
            print(f"run {run.status}: {run.error}", file=sys.stderr)
            return 1

        print(f"\n{run.result}\n")
        print(
            f"{run.usage.total_tokens} tokens "
            f"({run.usage.input_tokens} in, {run.usage.output_tokens} out) · "
            f"${run.usage.cost:.4f} · {run.duration_ms}ms"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RunboxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
