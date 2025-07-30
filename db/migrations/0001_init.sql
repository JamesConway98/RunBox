-- 0001_init.sql
-- Core system of record. Every tenant-scoped table carries tenant_id so that
-- row-level security (added in 0003) has something to key on.

create extension if not exists "pgcrypto";

create table tenants (
  id           uuid primary key default gen_random_uuid(),
  name         text not null,
  slug         text not null unique,
  created_at   timestamptz not null default now()
);

create table api_keys (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  key_hash     text not null unique,          -- sha256 of the key, never the key
  key_prefix   text not null,                 -- first 11 chars, for display
  name         text,
  created_at   timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at   timestamptz
);
create index api_keys_tenant_idx on api_keys (tenant_id);

create table runs (
  id            uuid primary key default gen_random_uuid(),
  tenant_id     uuid not null references tenants(id) on delete cascade,
  status        text not null
    check (status in ('queued','running','succeeded','failed','cancelled','timeout')),
  task          text not null,
  model         text not null,
  tools         text[] not null default '{}',
  system_prompt text,
  temperature   real,
  timeout_s     integer not null default 120,
  max_tokens    integer not null default 20000,
  result        text,
  error         text,
  created_at    timestamptz not null default now(),
  started_at    timestamptz,
  finished_at   timestamptz,
  duration_ms   integer
);
create index runs_tenant_created_idx on runs (tenant_id, created_at desc);
create index runs_status_idx on runs (status) where status in ('queued','running');

-- Append-only. Never updated, which is what makes it safe to stream and replay.
create table trace_events (
  id            bigserial primary key,
  run_id        uuid not null references runs(id) on delete cascade,
  tenant_id     uuid not null,
  seq           integer not null,             -- monotonic per run
  type          text not null,                -- llm_call|tool_call|tool_result|token|error|final
  payload       jsonb not null,
  created_at    timestamptz not null default now()
);
-- The uniqueness is load-bearing: it is what lets an SSE client resume from a
-- cursor without gaps or duplicates.
create unique index trace_events_run_seq_idx on trace_events (run_id, seq);

create table usage_records (
  id                uuid primary key default gen_random_uuid(),
  run_id            uuid not null references runs(id) on delete cascade,
  tenant_id         uuid not null,
  model             text not null,
  input_tokens      integer not null default 0,
  output_tokens     integer not null default 0,
  tool_calls        integer not null default 0,
  compute_ms        integer not null default 0,
  -- Integer micros. Money in floats is a bug waiting to happen.
  cost_micros       bigint  not null default 0,
  created_at        timestamptz not null default now()
);
create unique index usage_records_run_idx on usage_records (run_id);
create index usage_records_tenant_created_idx on usage_records (tenant_id, created_at desc);
