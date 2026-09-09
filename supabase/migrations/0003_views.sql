-- 0003_views.sql
-- Public read surface for the dashboard.
--
-- Both views are declared `security_invoker = true` so they run with the
-- PRIVILEGES OF THE CALLER, not the view owner. Without this, a view silently
-- bypasses the caller's RLS and column grants -- which would defeat 0002
-- entirely and re-expose the OPRA-licensed quote columns.
-- Requires Postgres 15+ (all new Supabase projects qualify).

-- ---------------------------------------------------------------------------
-- v_live_signals: today's signals, derived fields only.
-- Selecting bid/ask/mid here would fail for anon by design -- the columns are
-- not granted. Do not add them.
-- ---------------------------------------------------------------------------
create or replace view public.v_live_signals
with (security_invoker = true) as
select
  s.id,
  s.ts,
  s.module,
  s.model_version,
  s.contract_key,
  s.expiry,
  s.strike,
  s.option_right,
  s.predicted,
  s.edge_after_costs,
  s.confidence,
  s.action
from public.signals s
where s.ts >= (now() - interval '1 day')
order by s.ts desc;

-- ---------------------------------------------------------------------------
-- v_equity_curve: cumulative paper PnL per scenario.
-- Sourced from daily_metrics (already aggregated), never from paper_trades.
-- ---------------------------------------------------------------------------
create or replace view public.v_equity_curve
with (security_invoker = true) as
select
  d.trade_date,
  d.scenario,
  d.pnl                                              as daily_pnl,
  sum(d.pnl) over (
    partition by d.scenario
    order by d.trade_date
    rows between unbounded preceding and current row
  )                                                  as cum_pnl,
  d.n_trades,
  d.hit_rate
from public.daily_metrics d
order by d.trade_date;

grant select on public.v_live_signals to anon;
grant select on public.v_equity_curve to anon;
