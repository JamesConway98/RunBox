#!/usr/bin/env bash
#
# Apply migrations in lexical order, tracking what has already run.
#
# There is no migration framework here and that is deliberate: at this size a
# framework is more moving parts than it removes. What it does need is the one
# thing hand-rolled migrations usually get wrong — a record of what has already
# been applied, and each file in its own transaction so a failure is atomic.

set -euo pipefail

DATABASE_URL="${DATABASE_URL:-postgresql://runbox:runbox@localhost:5432/runbox}"
MIGRATIONS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../db/migrations" && pwd)"

psql_run() {
  psql "$DATABASE_URL" --quiet --no-psqlrc -v ON_ERROR_STOP=1 "$@"
}

psql_run -c "
  create table if not exists schema_migrations (
    version    text primary key,
    applied_at timestamptz not null default now()
  );" >/dev/null

applied="$(psql_run --tuples-only --no-align -c 'select version from schema_migrations')"

count=0
for file in "$MIGRATIONS_DIR"/*.sql; do
  version="$(basename "$file" .sql)"

  if grep -qxF "$version" <<<"$applied"; then
    continue
  fi

  echo "→ applying $version"
  # Single transaction per file: a migration either lands completely or not at
  # all. --single-transaction plus ON_ERROR_STOP is what buys that.
  psql_run --single-transaction \
    -f "$file" \
    -c "insert into schema_migrations (version) values ('$version')"
  count=$((count + 1))
done

if [ "$count" -eq 0 ]; then
  echo "already up to date"
else
  echo "applied $count migration(s)"
fi
