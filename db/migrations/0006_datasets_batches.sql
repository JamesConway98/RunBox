-- 0006_datasets_batches.sql
-- Datasets and batch runs.
--
-- A batch is a fan-out: one prompt template across every case in a dataset,
-- across every selected model. The runs it produces are ordinary runs — same
-- table, same runner, same trace stream — with a foreign key back to the batch.
-- Giving batches their own execution path would have meant maintaining two
-- engines that must not drift apart.

create table datasets (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  name         text not null,
  description  text,
  case_count   integer not null default 0,
  created_at   timestamptz not null default now()
);
create index datasets_tenant_created_idx on datasets (tenant_id, created_at desc);

create table dataset_cases (
  id           bigserial primary key,
  dataset_id   uuid not null references datasets(id) on delete cascade,
  tenant_id    uuid not null,
  idx          integer not null,        -- position in the uploaded file
  input        text not null,
  expected     text,                    -- optional, for exact-match scoring
  metadata     jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now()
);
create unique index dataset_cases_dataset_idx on dataset_cases (dataset_id, idx);

create table batches (
  id           uuid primary key default gen_random_uuid(),
  tenant_id    uuid not null references tenants(id) on delete cascade,
  dataset_id   uuid not null references datasets(id) on delete cascade,
  name         text not null,
  prompt_template text not null,        -- {{input}} is substituted per case
  models       text[] not null,
  tools        text[] not null default '{}',
  status       text not null default 'running'
    check (status in ('running','completed','cancelled')),
  total_runs   integer not null default 0,
  created_at   timestamptz not null default now(),
  finished_at  timestamptz
);
create index batches_tenant_created_idx on batches (tenant_id, created_at desc);

-- The link from a run back to the batch and case that produced it. Nullable,
-- because most runs are not part of a batch.
alter table runs add column batch_id uuid references batches(id) on delete cascade;
alter table runs add column case_id  bigint references dataset_cases(id) on delete set null;
create index runs_batch_idx on runs (batch_id) where batch_id is not null;

-- Live batch progress, derived rather than stored.
--
-- A counter column would need updating from the runner on every completion,
-- which is a write amplification and a source of drift the moment anything is
-- retried. At a few thousand runs per batch this aggregate is fast enough, and
-- it cannot disagree with reality.
create or replace function runbox_batch_progress(p_batch uuid)
returns table (
  total       bigint,
  completed   bigint,
  failed      bigint,
  in_flight   bigint,
  cost_micros bigint
)
language sql
stable
as $$
  select
    count(*)::bigint,
    count(*) filter (where r.status = 'succeeded')::bigint,
    count(*) filter (where r.status in ('failed','timeout','cancelled'))::bigint,
    count(*) filter (where r.status in ('queued','running'))::bigint,
    coalesce(sum(u.cost_micros), 0)::bigint
  from runs r
  left join usage_records u on u.run_id = r.id
  where r.batch_id = p_batch;
$$;

alter table datasets      enable row level security;
alter table dataset_cases enable row level security;
alter table batches       enable row level security;
alter table datasets      force row level security;
alter table dataset_cases force row level security;
alter table batches       force row level security;

create policy datasets_isolation on datasets
  using (tenant_id = runbox_current_tenant())
  with check (tenant_id = runbox_current_tenant());

create policy dataset_cases_isolation on dataset_cases
  using (tenant_id = runbox_current_tenant())
  with check (tenant_id = runbox_current_tenant());

create policy batches_isolation on batches
  using (tenant_id = runbox_current_tenant())
  with check (tenant_id = runbox_current_tenant());

grant select, insert, update, delete on datasets to runbox_api;
grant select, insert, delete on dataset_cases to runbox_api;
grant select, insert, update on batches to runbox_api;
grant usage, select on all sequences in schema public to runbox_api;
grant select, insert, update on all tables in schema public to runbox_runner;
