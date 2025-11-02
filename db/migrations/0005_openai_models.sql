-- 0005_openai_models.sql
-- A second provider, so "multi-model behind an interface" is a fact rather
-- than an intention.
--
-- Nothing about the schema changes, which is the point: `provider` was already
-- a column, the agent already selected an implementation, and adding a vendor
-- turned out to be rows plus one Python module. That is the return on having
-- built the seam early.

insert into model_pricing
  (model, display_name, provider, context_length,
   input_micros_per_1k, output_micros_per_1k, compute_micros_per_s, supports_tools)
values
  ('gpt-4o',       'GPT-4o',       'openai', 128000, 2500,  10000, 200, true),
  ('gpt-4o-mini',  'GPT-4o mini',  'openai', 128000,  150,    600, 200, true)
on conflict (model) do update set
  display_name         = excluded.display_name,
  provider             = excluded.provider,
  context_length       = excluded.context_length,
  input_micros_per_1k  = excluded.input_micros_per_1k,
  output_micros_per_1k = excluded.output_micros_per_1k;
