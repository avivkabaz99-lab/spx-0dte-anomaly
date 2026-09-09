# Decision log

Append-only. Each entry records what was decided, why, and what would reverse it.

---

## 2026-09-09 — ML target is a residual, not a price

**Decided:** Module B predicts `log(RV_remaining / IV_current)`; Module A predicts
the change in a strike's deviation from the fitted IV surface.

**Why:** predicting the option price directly relearns Black-Scholes. High R²,
zero edge.

**Reverses if:** never, for this project.

---

## 2026-09-09 — Module B before Module A

**Decided:** build the variance-risk-premium module first.

**Why:** Module B needs only SPX index bars (IBKR, years of history) and CBOE
VIX1D — both available immediately. Module A needs recorded option chains that do
not exist yet. Building B first means the full pipeline is exercised while data
accumulates, instead of waiting.

**Reverses if:** step 1 shows IBKR can in fact serve historical 0DTE chains.

---

## 2026-09-09 — Storage is Parquet + Supabase, not Supabase alone

**Decided:** raw chain snapshots to Parquet outside git; Postgres holds only
derived artifacts.

**Why:** ~280k rows/day (~28 MB) would exhaust the Supabase free tier in ~18
days, and Parquet reads far faster for backtesting than SQL. Also required by the
OPRA licence — see below.

---

## 2026-09-09 — Public repo, with licence-driven exposure limits

**Decided:** `avivkabaz99-lab/spx-0dte-anomaly` is public. Raw quotes are
excluded from both the repo and the public read surface.

**Why:** the OPRA subscriber agreement prohibits redistributing received market
data. A public GitHub Pages site serving live bid/ask is redistribution.
Enforced by column-level grants in `0002_rls.sql` and by `tests/test_rls.py`.

**Employer policy:** checked by the user before the repo was created.

---

## 2026-09-09 — IBKR stays read-only

**Decided:** `BROKER_MODE=null` by default; `risk_state.kill_switch` defaults to
true; `IBKRBroker` is not implemented.

**Why:** standing rule for this machine. Live execution is an explicit, separate
decision.

---

## OPEN — verify IBKR historical behaviour for 0DTE (roadmap step 1)

Two claims drive the whole data plan and are **assumed, not yet measured**:

1. `Contract.includeExpired` is documented as futures / futures-options only, so
   expired SPX options are not queryable.
2. Historical pacing (~60 req / 10 min) makes bulk chain history impractical.

Test both against the live API and record the result here. Half a day of work
that determines whether steps 2–7 are ordered correctly.

---

## 2026-09-09 — Broker client is the official `ibapi`, not `ib_insync`

**Decided:** use IBKR's official Python API (`ibapi`, TWS API 10.45).

**Why:** `ib_insync` was archived in 2024 and `ib_async` is a third-party fork;
the official client has no maintainer risk and tracks new API features first.
It is also the client used professionally at an IBKR introducing broker.

**Cost accepted:** `ibapi` is callback-based (`EWrapper`/`EClient` plus a reader
thread), not `async`/`await`. `ingest/` therefore carries a thin adapter that
turns callbacks into plain data structures; nothing outside `ingest/` and
`execution/` should import `ibapi` directly.

**Install:** PyPI's `ibapi` is a stale 9.81 mirror, and the API is under the IB
API Non-Commercial License, so the source is not vendored into this public
repo. `scripts/install_ibapi.sh` downloads the pinned official zip, verifies its
SHA-256, and installs the Python client into `.venv`.

**Reverses if:** IBKR publishes a current `ibapi` on PyPI (then drop the script).
