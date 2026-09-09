-- 0001_init_schema.sql
-- Core schema for the SPX 0DTE anomaly system.
--
-- Storage split (see SPEC.md):
--   Raw option-chain snapshots live in Parquet OUTSIDE this database.
--   Postgres holds only derived artifacts: signals, trades, metrics, model runs.
--   snapshots_meta is the index that points at the Parquet files.

-- ---------------------------------------------------------------------------
-- snapshots_meta: one row per recorded chain snapshot
-- ---------------------------------------------------------------------------
create table if not exists public.snapshots_meta (
  id            bigint generated always as identity primary key,
  ts            timestamptz not null,
  spot          numeric(12,4) not null,
  parquet_path  text          not null,
  n_contracts   integer       not null,
  created_at    timestamptz   not null default now(),
  constraint snapshots_meta_ts_key      unique (ts),
  constraint snapshots_meta_spot_pos    check (spot > 0),
  constraint snapshots_meta_n_pos       check (n_contracts > 0)
);

create index if not exists snapshots_meta_ts_idx
  on public.snapshots_meta (ts desc);

-- ---------------------------------------------------------------------------
-- surface_fits: one row per SVI fit (Module A)
-- ---------------------------------------------------------------------------
create table if not exists public.surface_fits (
  id          bigint generated always as identity primary key,
  ts          timestamptz not null,
  expiry      date        not null,
  params      jsonb       not null,
  rmse        numeric(8,5) not null,
  arb_free    boolean      not null,
  created_at  timestamptz  not null default now(),
  constraint surface_fits_ts_expiry_key unique (ts, expiry),
  constraint surface_fits_rmse_pos      check (rmse >= 0)
);

create index if not exists surface_fits_ts_idx
  on public.surface_fits (ts desc);

-- ---------------------------------------------------------------------------
-- model_runs: training provenance
-- ---------------------------------------------------------------------------
create table if not exists public.model_runs (
  id             bigint generated always as identity primary key,
  module         text        not null,
  version        text        not null,
  trained_at     timestamptz not null default now(),
  train_start    date        not null,
  train_end      date        not null,
  metrics        jsonb       not null default '{}'::jsonb,
  artifact_path  text,
  constraint model_runs_module_check  check (module in ('A','B')),
  constraint model_runs_version_key   unique (module, version),
  constraint model_runs_range_check   check (train_end >= train_start)
);

-- ---------------------------------------------------------------------------
-- signals: model output. THE OPRA-SENSITIVE TABLE.
--
-- bid / ask / mid are raw OPRA-derived quotes. They are needed for backtesting
-- and PnL, but must never be readable by the `anon` role -- redistributing raw
-- market data violates the OPRA subscriber agreement. Column-level grants in
-- 0002_rls.sql enforce this at the database level.
-- ---------------------------------------------------------------------------
create table if not exists public.signals (
  id                bigint generated always as identity primary key,
  ts                timestamptz not null,
  module            text        not null,
  model_version     text        not null,

  -- contract identity
  contract_key      text        not null,
  expiry            date        not null,
  strike            numeric(10,2),
  option_right      text,

  -- model output (safe to publish: derived, not raw quotes)
  predicted         numeric(14,6) not null,
  edge_raw          numeric(14,6) not null,
  edge_after_costs  numeric(14,6) not null,
  confidence        numeric(5,4)  not null,
  action            text          not null,

  -- RAW QUOTES -- restricted, see above
  bid               numeric(12,4),
  ask               numeric(12,4),
  mid               numeric(12,4),

  created_at        timestamptz not null default now(),

  constraint signals_module_check     check (module in ('A','B')),
  constraint signals_right_check      check (option_right is null or option_right in ('C','P')),
  constraint signals_action_check     check (action in ('buy','sell','none')),
  constraint signals_confidence_check check (confidence >= 0 and confidence <= 1),
  constraint signals_strike_check     check (strike is null or strike > 0),
  constraint signals_spread_check     check (bid is null or ask is null or ask >= bid)
);

-- Dashboard reads "latest signals", optionally filtered by module.
-- Equality column first, range column last (leftmost-prefix rule).
create index if not exists signals_module_ts_idx
  on public.signals (module, ts desc);
create index if not exists signals_ts_idx
  on public.signals (ts desc);

-- ---------------------------------------------------------------------------
-- paper_trades: simulated fills, linked back to the signal that caused them
-- ---------------------------------------------------------------------------
create table if not exists public.paper_trades (
  id           bigint generated always as identity primary key,
  signal_id    bigint      not null references public.signals (id) on delete restrict,
  ts_open      timestamptz not null,
  ts_close     timestamptz,
  qty          integer     not null,
  entry        numeric(12,4) not null,
  exit         numeric(12,4),
  pnl          numeric(14,4),
  exit_reason  text,
  scenario     text        not null default 'realistic',
  created_at   timestamptz not null default now(),
  constraint paper_trades_qty_nonzero   check (qty <> 0),
  constraint paper_trades_scenario_check
    check (scenario in ('optimistic','realistic','pessimistic')),
  constraint paper_trades_close_check
    check (ts_close is null or ts_close >= ts_open)
);

-- Foreign keys are not indexed automatically; joins and cascades need this.
create index if not exists paper_trades_signal_id_idx
  on public.paper_trades (signal_id);
create index if not exists paper_trades_ts_open_idx
  on public.paper_trades (ts_open desc);

-- ---------------------------------------------------------------------------
-- daily_metrics: one row per trading day per scenario
-- ---------------------------------------------------------------------------
create table if not exists public.daily_metrics (
  trade_date  date    not null,
  scenario    text    not null,
  n_signals   integer not null default 0,
  n_trades    integer not null default 0,
  pnl         numeric(14,4) not null default 0,
  hit_rate    numeric(5,4),
  sharpe      numeric(8,4),
  updated_at  timestamptz not null default now(),
  primary key (trade_date, scenario),
  constraint daily_metrics_scenario_check
    check (scenario in ('optimistic','realistic','pessimistic')),
  constraint daily_metrics_hit_rate_check
    check (hit_rate is null or (hit_rate >= 0 and hit_rate <= 1))
);

-- ---------------------------------------------------------------------------
-- risk_state: singleton row read by the execution layer on every tick.
-- Exists now so the kill switch is never retrofitted later.
-- ---------------------------------------------------------------------------
create table if not exists public.risk_state (
  id               smallint primary key default 1,
  kill_switch      boolean       not null default true,
  max_positions    integer       not null default 0,
  max_daily_loss   numeric(12,2) not null default 0,
  max_order_size   integer       not null default 0,
  updated_at       timestamptz   not null default now(),
  constraint risk_state_singleton check (id = 1)
);

-- Default state is deliberately locked: kill switch ON, all limits zero.
insert into public.risk_state (id) values (1) on conflict (id) do nothing;
