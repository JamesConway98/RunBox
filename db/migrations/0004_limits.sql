-- 0004_limits.sql
-- Per-tenant quotas, checked at enqueue time.
--
-- This is what makes metering a system rather than a counter: the numbers are
-- not just recorded, they are load-bearing.

create table tenant_limits (
  tenant_id            uuid primary key references tenants(id) on delete cascade,
  monthly_token_ceiling  bigint,   -- null means unlimited
  monthly_cost_ceiling_micros bigint,
  max_concurrent_runs  integer not null default 8,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now()
);

alter table tenant_limits enable row level security;
alter table tenant_limits force row level security;
grant select on tenant_limits to runbox_api;

create policy tenant_limits_isolation on tenant_limits
  using (tenant_id = runbox_current_tenant());

-- Current calendar month's consumption for one tenant.
--
-- A function rather than a view so the tenant is an explicit argument. The
-- enqueue path needs this on every create, and a sequential scan of
-- usage_records would show up long before anything else does — hence the index
-- below, which the (tenant_id, created_at desc) index already partly serves
-- but not for the month-boundary predicate.
create or replace function runbox_month_usage(p_tenant uuid)
returns table (total_tokens bigint, total_cost_micros bigint, run_count bigint)
language sql
stable
as $$
  select
    coalesce(sum(input_tokens + output_tokens), 0)::bigint,
    coalesce(sum(cost_micros), 0)::bigint,
    count(*)::bigint
  from usage_records
  where tenant_id = p_tenant
    and created_at >= date_trunc('month', now());
$$;

create index usage_records_tenant_month_idx
  on usage_records (tenant_id, created_at)
  include (input_tokens, output_tokens, cost_micros);

-- The public demo tenant gets a real ceiling, because it is the one tenant
-- whose traffic is genuinely unpredictable.
insert into tenant_limits (tenant_id, monthly_token_ceiling, max_concurrent_runs)
select id, 2000000, 2 from tenants where slug = 'demo'
on conflict (tenant_id) do nothing;
