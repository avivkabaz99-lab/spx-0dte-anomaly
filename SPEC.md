# SPX 0DTE Anomaly System — Specification

Research system for detecting and evaluating mispricing in same-day-expiry SPX
options. Currently **research and paper only**: the IBKR connection is read-only
and no order ever reaches a venue.

---

## 1. What "anomaly" means here

A model that predicts an option's *price* is not useful. Price is ~99.9%
determined by spot, strike, time to expiry, IV and rate — such a model relearns
Black-Scholes, reports R² of 0.999, and carries no edge. **The target must be a
residual, not a price.**

Three levels of anomaly, only two of which are modelled:

| Level | What it is | Role in this system |
|---|---|---|
| **Hard arbitrage** | Put-call parity violations, sub-risk-free box spreads, negative-price butterflies, non-monotonic verticals | **Data-quality validator, not a strategy.** Deterministic; needs no ML. If the system "finds" many of these, the data is stale or unsynchronised. |
| **IV surface residual** | Fit a smile to the chain; deviation from the curve is the candidate | **Module A** |
| **Variance risk premium** | Forecast realized vol over the remaining session against implied | **Module B** |

## 2. Why 0DTE is hard

- **The spread is the edge.** An SPX 0DTE option quoted 1.00/1.40 loses ~33% of
  premium to a round trip. Any signal evaluated against mid is fiction.
- **Settlement differs.** SPXW (daily/weekly) is PM-settled; monthly SPX is
  AM-settled on SET. Mixing them contaminates expiry-week data.
- **Vega collapses near the close.** In the last ~30 minutes a one-cent move can
  swing implied vol by 10 points, so IV inversion becomes unusable. Work in
  total-variance or price space past the cutoff.
- **European and cash-settled** — the one simplification. No early exercise, no
  assignment risk, unlike SPY.

## 3. Data

IBKR cannot supply 0DTE history retroactively:

1. `includeExpired` is documented for futures and futures options only, not index
   options — and a 0DTE contract is dead by the end of its trading day.
2. Historical pacing is ~60 requests / 10 minutes. A 1000+ contract chain takes
   ~3 hours per day of history.

**So "pull IBKR history" and "record it yourself" are the same thing for 0DTE.**
This is verified empirically in step 1 of the roadmap before anything is built on
it; findings go in `DECISIONS.md`.

Module B sidesteps the problem entirely:

| Input | Source | Availability |
|---|---|---|
| SPX 1-min bars, years back | IBKR `reqHistoricalData` on the index | immediate |
| Implied vol for 0DTE | CBOE **VIX1D**, free history | immediate |
| VIX9D / VIX term structure | CBOE, free | immediate |

Module B therefore ships first, on data that already exists, while the recorder
accumulates chains for Module A.

## 4. Modules

### Module B — Variance Risk Premium (first)

- **Target:** `log(RV_remaining / IV_current)` over the rest of the session.
- **Features:** RV over 5/15/30/60-min windows · overnight gap · minutes to close ·
  VIX1D level and Δ · term structure (VIX1D/VIX9D/VIX) · SPX return and trend ·
  day of week · macro event flag (FOMC/CPI/NFP) · prior-day RV/IV spread.
- **Model:** LightGBM regression. Not a neural network — the dataset is small,
  the features are tabular, and feature importance is needed to understand the
  result. Deep learning here would overfit and explain nothing.
- **Signal:** IV too high → short premium; IV too low → long premium.

### Module A — IV Surface Residual (second)

- **Pipeline:** chain → IV inversion → SVI fit (single expiry, so only the
  Gatheral-Jacquier butterfly constraint applies) → per-strike residual.
- **Target:** `residual_{t+N} − residual_t` — does the deviation mean-revert.
- **Features:** log-moneyness · time to expiry · residual · z-score of the
  residual against its own recent history · bid-ask spread as % of premium ·
  volume/OI · quote age · neighbouring residuals.

## 5. Backtesting rule

Every result is reported under **three execution scenarios**:

| Scenario | Assumption |
|---|---|
| Optimistic | fill at mid |
| Realistic | fill at mid ± 25% of the spread |
| Pessimistic | buy at ask, sell at bid |

Plus IBKR commissions (~$1–1.5/contract round trip) and exchange fees.
**A strategy profitable only under `optimistic` does not exist.** This is
asserted in the test suite, not noted in a README.

## 6. Storage

| Data | Where | Why |
|---|---|---|
| Raw chain snapshots | Parquet, partitioned by date, **outside git** | ~280k rows/day would exhaust the Supabase free tier in ~18 days; also an OPRA licence requirement |
| Signals, trades, metrics, model runs | Supabase Postgres | small, and the dashboard reads it live |

## 7. Market data licensing (constrains the public dashboard)

The OPRA subscriber agreement prohibits redistributing received market data.
Consequences, enforced in code:

- `data/` and `*.parquet` are gitignored — raw quotes never enter the repo.
- `signals.bid`, `signals.ask` and `signals.mid` are **not granted** to the
  `anon` role (`supabase/migrations/0002_rls.sql`). Column-level grants make them
  unreachable even by a crafted query.
- `paper_trades` is fully private; entry/exit are fill prices, i.e. quote-derived.
- The public dashboard shows derived values only: model output, edge estimates,
  confidence, aggregated PnL.
- `tests/test_rls.py` fails if any of this regresses.

## 8. Architecture

```
src/spx0dte/
├── ingest/      recorder.py (chain → Parquet), index_history.py, cboe.py
│                ibkr_adapter.py wraps the callback-based official `ibapi`
│                client; nothing else imports `ibapi` directly
├── pricing/     Black-Scholes, greeks, IV solver, parity + butterfly checks
├── surface/     SVI fit + residuals
├── features/    feature engineering for both modules
├── models/      module_a/, module_b/
├── backtest/    event-driven engine + execution model
├── execution/   BrokerInterface protocol; NullBroker today
└── db/          Supabase client
web/             HTML + CSS + supabase-js, served by GitHub Pages
supabase/migrations/
```

### Execution layer

```
BrokerInterface (Protocol)
├── NullBroker    records intent, places nothing        ← default, read-only
├── PaperBroker   simulated fills against recorded bid/ask   (step 5)
└── IBKRBroker    official ibapi, behind an env flag, disabled (not before live)
```

`RiskManager` — max positions, max daily loss, max order size, trading-hours
window, and a kill switch read from `risk_state` on every tick — lands with
`PaperBroker`. The `risk_state` table already exists and defaults to
**kill switch ON, all limits zero**, so the safe state is never retrofitted.

Switching modes is one line in `.env` (`BROKER_MODE`), never an architecture change.

## 9. Roadmap

| # | Step | Verification |
|---|---|---|
| 0 | Repo scaffold, Supabase schema, RLS | `git check-ignore -v .env` passes; `anon` cannot select `bid` |
| 1 | Spike: what does IBKR actually return for 0DTE, live and expired? | Unambiguous answer recorded in `DECISIONS.md` |
| 2 | Recorder on cron | 5 consecutive trading days, zero gaps |
| 3 | Module B end-to-end | Beats the naive `IV = RV` baseline out-of-sample |
| 4 | Backtest engine + 3 scenarios | One strategy, three numbers, all reported |
| 5 | Dashboard + paper portfolio + RiskManager | signal → trade → PnL end-to-end in the browser |
| 6 | Pricing engine + parity checks | pytest against known BS values; parity violations < 0.5% of chain |
| 7 | Module A | SVI fit RMSE < 0.5 vol pts at ATM, arbitrage-free |

Steps 1–2 are on the critical path for time: every day without a recorder is a
day missing from the dataset. Steps 3–6 run in parallel with accumulation.
