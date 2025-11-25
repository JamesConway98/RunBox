-- 0007_evals.sql
-- Scoring.
--
-- One row per (run, scorer). A run can be scored by several scorers, and a
-- scorer can be re-run after its config changes, so the natural key is the pair
-- plus a version rather than the run alone.

create table eval_scores (
  id           bigserial primary key,
  tenant_id    uuid not null,
  run_id       uuid not null references runs(id) on delete cascade,
  batch_id     uuid references batches(id) on delete cascade,
  scorer       text not null,            -- exact_match|contains|regex|latency|llm_judge
  passed       boolean not null,
  score        real not null default 0,  -- 0..1, so scorers compose into an average
  detail       text,                     -- why, in one line, for the UI
  -- The judge run, when the scorer was llm_judge. Judging with the platform
  -- itself means judge runs are traced, metered and cancellable like any other.
  judge_run_id uuid references runs(id) on delete set null,
  created_at   timestamptz not null default now()
);

create unique index eval_scores_run_scorer_idx on eval_scores (run_id, scorer);
create index eval_scores_batch_idx on eval_scores (batch_id) where batch_id is not null;
create index eval_scores_tenant_created_idx on eval_scores (tenant_id, created_at desc);

alter table eval_scores enable row level security;
alter table eval_scores force row level security;

create policy eval_scores_isolation on eval_scores
  using (tenant_id = runbox_current_tenant())
  with check (tenant_id = runbox_current_tenant());

grant select, insert, update, delete on eval_scores to runbox_api;

-- Pass rate and cost per model for a batch, which is the chart the evals page
-- draws. Doing the join here rather than in the API keeps the endpoint to one
-- round trip.
create or replace function runbox_batch_scores(p_batch uuid)
returns table (
  model         text,
  scorer        text,
  total         bigint,
  passed        bigint,
  avg_score     double precision,
  avg_latency_ms double precision,
  cost_micros   bigint
)
language sql
stable
as $$
  select
    r.model,
    s.scorer,
    count(*)::bigint,
    count(*) filter (where s.passed)::bigint,
    avg(s.score)::double precision,
    avg(r.duration_ms)::double precision,
    coalesce(sum(u.cost_micros), 0)::bigint
  from eval_scores s
  join runs r on r.id = s.run_id
  left join usage_records u on u.run_id = r.id
  where s.batch_id = p_batch
  group by r.model, s.scorer
  order by r.model, s.scorer;
$$;
