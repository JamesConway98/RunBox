-- 0002_pricing.sql
-- Cost is computed at write time from the pricing row in force and stored.
-- Prices change; historical costs must not.

create table model_pricing (
  model                  text primary key,
  display_name           text not null,
  provider               text not null,
  context_length         integer not null,
  input_micros_per_1k    bigint not null,
  output_micros_per_1k   bigint not null,
  compute_micros_per_s   bigint not null default 0,
  supports_tools         boolean not null default true,
  active                 boolean not null default true,
  created_at             timestamptz not null default now()
);

insert into model_pricing
  (model, display_name, provider, context_length,
   input_micros_per_1k, output_micros_per_1k, compute_micros_per_s)
values
  ('claude-sonnet-5',    'Claude Sonnet 5',   'anthropic', 200000,  3000, 15000, 200),
  ('claude-haiku-4-5',   'Claude Haiku 4.5',  'anthropic', 200000,   800,  4000, 200),
  ('claude-opus-5',      'Claude Opus 5',     'anthropic', 200000, 15000, 75000, 200);
