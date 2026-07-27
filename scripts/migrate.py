#!/usr/bin/env python3
"""Apply migrations in lexical order, tracking what has already run.

Replaces the earlier bash+psql version. Not because bash was wrong, but because
psql is a system dependency that has to be installed separately on macOS, while
asyncpg is already a dependency of the control plane. One fewer thing that has
to be true before someone can run this.

Still no migration framework. What this needs is the one thing hand-rolled
migrations usually get wrong — a record of what has been applied, and each file
in its own transaction so a failure is atomic — and that is about forty lines.

    DATABASE_URL=postgresql://... python scripts/migrate.py
    python scripts/migrate.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

BOOTSTRAP = """
create table if not exists schema_migrations (
  version    text primary key,
  applied_at timestamptz not null default now()
)
"""


async def main() -> int:
    parser = argparse.ArgumentParser(description="Apply Runbox migrations.")
    parser.add_argument(
        "--dry-run", action="store_true", help="List what would run, change nothing."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Defaults to $DATABASE_URL.",
    )
    args = parser.parse_args()

    if not args.database_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"No migrations found in {MIGRATIONS_DIR}", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(args.database_url)
    try:
        await conn.execute(BOOTSTRAP)
        applied = {r["version"] for r in await conn.fetch("select version from schema_migrations")}

        pending = [f for f in files if f.stem not in applied]
        if not pending:
            print("already up to date")
            return 0

        if args.dry_run:
            for path in pending:
                print(f"would apply {path.stem}")
            return 0

        for path in pending:
            print(f"→ applying {path.stem}")
            # One transaction per file: a migration either lands completely or
            # not at all. asyncpg sends a multi-statement string over the simple
            # query protocol, which is also what lets the `do $$ ... $$` blocks
            # and function bodies in these files work unmodified.
            try:
                async with conn.transaction():
                    await conn.execute(path.read_text())
                    await conn.execute(
                        "insert into schema_migrations (version) values ($1)", path.stem
                    )
            except asyncpg.PostgresError as exc:
                # Name the file and the position. "syntax error at or near" with
                # no indication of which of eight files it came from is a bad
                # half hour.
                print(f"\n{path.name} failed: {exc}", file=sys.stderr)
                if position := getattr(exc, "position", None):
                    print(f"  at character {position}", file=sys.stderr)
                return 1

        print(f"applied {len(pending)} migration(s)")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
