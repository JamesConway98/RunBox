-- 0003_rls.sql
-- Row-level security.
--
-- The application already filters by tenant_id in every query. This exists
-- because "we remembered the where clause everywhere" is a promise about human
-- attention, and one missing predicate in one endpoint is a cross-tenant data
-- leak. RLS makes it a property of the database instead.
--
-- The tenant travels on the connection as a GUC, set with is_local => true so
-- it is scoped to the transaction and cannot survive a return to the pool.

create or replace function runbox_current_tenant() returns uuid
language plpgsql
stable
as $$
declare
  raw text := current_setting('runbox.tenant_id', true);
begin
  if raw is null or raw = '' then
    return null;
  end if;
  return raw::uuid;
exception
  -- A malformed setting must not become "no filter". Returning null means the
  -- policies below match nothing, which is the safe direction to fail in.
  when invalid_text_representation then
    return null;
end;
$$;

-- The role the API connects as. It deliberately does not own the tables, so
-- RLS actually applies to it — a table owner bypasses policies by default.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'runbox_api') then
    create role runbox_api login password 'runbox_api';
  end if;
end
$$;

grant usage on schema public to runbox_api;
grant select, insert, update on runs to runbox_api;
grant select, insert on trace_events to runbox_api;
grant select on usage_records to runbox_api;
grant select on model_pricing to runbox_api;
grant select on tenants to runbox_api;
grant usage, select on all sequences in schema public to runbox_api;

alter table runs           enable row level security;
alter table trace_events   enable row level security;
alter table usage_records  enable row level security;

-- FORCE so that even a table owner is subject to the policy. Without it, a
-- migration or an admin script connected as the owner sees everything, which
-- is exactly the habit that produces a leak later.
alter table runs           force row level security;
alter table trace_events   force row level security;
alter table usage_records  force row level security;

create policy runs_tenant_isolation on runs
  using (tenant_id = runbox_current_tenant())
  with check (tenant_id = runbox_current_tenant());

create policy trace_events_tenant_isolation on trace_events
  using (tenant_id = runbox_current_tenant())
  with check (tenant_id = runbox_current_tenant());

create policy usage_records_tenant_isolation on usage_records
  using (tenant_id = runbox_current_tenant())
  with check (tenant_id = runbox_current_tenant());

-- The runner is not tenant-scoped: it executes work for every tenant and needs
-- to write traces for all of them. It gets its own role that bypasses RLS,
-- rather than the API's role being loosened to accommodate it.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'runbox_runner') then
    create role runbox_runner login password 'runbox_runner' bypassrls;
  end if;
end
$$;

grant usage on schema public to runbox_runner;
grant select, insert, update on all tables in schema public to runbox_runner;
grant usage, select on all sequences in schema public to runbox_runner;

comment on function runbox_current_tenant() is
  'Reads runbox.tenant_id from the session. Returns null when unset or '
  'malformed, so policies match nothing rather than everything.';
