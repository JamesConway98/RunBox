# Database

Postgres 16. Migrations are plain, forward-only `.sql` files applied in
lexical order. There is no migration framework and that is deliberate: at this
size a framework is more moving parts than it removes.

```bash
make db-migrate          # apply every migration in order
make db-seed             # demo tenant + api key + a few finished runs
```

`schema.sql` is a generated snapshot of the resulting schema, committed so that
a reader can see the whole shape without replaying migrations.

## Conventions

- Every tenant-scoped table carries `tenant_id`, not a join away.
- Money is `bigint` micros. Never floats, never numeric-with-rounding-at-read.
- `trace_events` is append-only. Nothing updates a row after insert.
- Timestamps are `timestamptz`, always. The runner and the control plane may
  well be in different regions.
