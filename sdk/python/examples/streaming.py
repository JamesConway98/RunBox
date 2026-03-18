"""Render a live trace in the terminal.

python examples/streaming.py "Summarise the three most recent Go releases"
"""

import sys

from runbox import Runbox, RunboxError

DIM, CYAN, GREEN, RED, RESET = "\033[2m", "\033[36m", "\033[32m", "\033[31m", "\033[0m"


def main() -> int:
    task = " ".join(sys.argv[1:]) or "What are the three most recent releases of Go?"

    with Runbox() as rb:
        run = rb.runs.create(task=task, tools=["http_get"])
        print(f"{DIM}run {run.id}{RESET}\n")

        for event in run.stream(
            on_reconnect=lambda n, seq: print(
                f"\n{DIM}reconnecting (attempt {n}, from seq {seq}){RESET}", file=sys.stderr
            )
        ):
            if event.type == "llm_call":
                print(f"{DIM}· calling {event.payload.get('model')}{RESET}")
            elif event.type == "tool_call":
                print(f"{CYAN}→ {event.tool}({event.args}){RESET}")
            elif event.type == "tool_result":
                colour = GREEN if event.ok else RED
                ms = event.payload.get("duration_ms", 0)
                print(f"{colour}← {event.tool} {'ok' if event.ok else 'failed'} in {ms}ms{RESET}")
            elif event.type == "token":
                # Tokens print as they arrive, unbuffered — that is the point.
                print(event.text, end="", flush=True)
            elif event.type == "error":
                print(f"\n{RED}! {event.message}{RESET}")

        print(
            f"\n\n{DIM}{run.status} · {run.usage.total_tokens} tokens · "
            f"${run.usage.cost:.4f} · {run.duration_ms}ms{RESET}"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted (the run continues server-side)", file=sys.stderr)
        sys.exit(130)
    except RunboxError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
