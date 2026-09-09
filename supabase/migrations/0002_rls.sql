-- 0002_rls.sql
-- Access control. Two independent layers guard the OPRA-licensed columns:
--
--   1. Column-level GRANTs -- `anon` is not granted SELECT on signals.bid/ask/mid
--      at all, so the columns are unreachable even via a crafted query.
--   2. RLS policies        -- control which ROWS each role sees.
--
-- Layer 1 is the one that satisfies the OPRA subscriber agreement. Layer 2 is
-- defence in depth. Supabase's default privileges grant ALL on new public
-- tables to anon/authenticated, so every table below is revoked first.
--
-- Note: FORCE ROW LEVEL SECURITY is deliberately NOT used. It would also apply
-- to the table owner, which makes the Supabase SQL editor appear to return
-- empty tables -- a confusing footgun for no gain here, since `anon` is
-- constrained by grants rather than by owner-level policy.

-- ---------------------------------------------------------------------------
-- Enable RLS everywhere
-- ---------------------------------------------------------------------------
alter table public.snapshots_meta enable row level security;
alter table public.surface_fits   enable row level security;
alter table public.model_runs     enable row level security;
alter table public.signals        enable row level security;
alter table public.paper_trades   enable row level security;
alter table public.daily_metrics  enable row level security;
alter table public.risk_state     enable row level security;

-- ---------------------------------------------------------------------------
-- Strip Supabase's default grants from the public roles
-- ---------------------------------------------------------------------------
revoke all on table public.snapshots_meta from anon, authenticated;
revoke all on table public.surface_fits   from anon, authenticated;
revoke all on table public.model_runs     from anon, authenticated;
revoke all on table public.signals        from anon, authenticated;
revoke all on table public.paper_trades   from anon, authenticated;
revoke all on table public.daily_metrics  from anon, authenticated;
revoke all on table public.risk_state     from anon, authenticated;

-- ---------------------------------------------------------------------------
-- signals: anon may read DERIVED columns only.
-- bid, ask and mid are intentionally absent from this grant list.
-- ---------------------------------------------------------------------------
grant select (
  id,
  ts,
  module,
  model_version,
  contract_key,
  expiry,
  strike,
  option_right,
  predicted,
  edge_raw,
  edge_after_costs,
  confidence,
  action
) on table public.signals to anon;

create policy signals_anon_read
  on public.signals
  for select
  to anon
  using (true);

-- ---------------------------------------------------------------------------
-- daily_metrics: fully aggregated, safe to publish in full.
-- ---------------------------------------------------------------------------
grant select on table public.daily_metrics to anon;

create policy daily_metrics_anon_read
  on public.daily_metrics
  for select
  to anon
  using (true);

-- ---------------------------------------------------------------------------
-- Everything else stays private. No grants, no policies for anon:
--   snapshots_meta -- points at raw OPRA data on disk
--   surface_fits   -- fitted IV surface, derived from raw quotes
--   model_runs     -- training provenance
--   paper_trades   -- entry/exit are fill prices, i.e. quote-derived
--   risk_state     -- execution control surface, never public
--
-- The backend writer uses the service_role key, which has BYPASSRLS.
