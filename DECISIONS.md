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

## 2026-09-09 — IBKR historical behaviour for 0DTE, measured (closes the OPEN item)

Measured with `scripts/spike_historical.py` against **IB Gateway on 4001, a live
account, read-only**. The adapter refuses to place orders.

**Assumption 1 was wrong, but not in the direction that would reorder the roadmap.**

| Question | Measured |
|---|---|
| Expired SPXW resolvable with `includeExpired=True`? | **Yes.** 20260908 7675C, conId 907134620, 14 bars of 30 min, 13 distinct closes, volume 28,499 |
| How far back? | **One trading day.** Of six weekdays probed, only 20260908 resolved; 20260904 and 20260903 are ordinary trading days and returned error 200 |
| Today's chain | 242 strikes, 3200-10000 |
| Yesterday's chain | 250 strikes, 3000-10000 |
| Pacing, 65 sequential historical requests | **No pushback.** 65/65 round trips in 89 s, 44/min, 7 empty |

Controls were run before believing the first row: a strike that never existed
(99999C) and an invented expiry (20200101) both return error 200, so TWS does not
fabricate data for a contract it does not know.

**What this changes:** nothing about module order. Module A still cannot be
trained on a historical chain, because the window is one day and not one year, so
**B before A stands**. What it does add is a one-trading-day safety net: if the
recorder misses a session, the full chain can still be backfilled the next
morning, and only the next morning. That makes step 2 more valuable, not less,
and it makes a recorder failure recoverable for exactly one day.

**What would reverse this:** IBKR extending option history retention, or a
different data source for expired chains. Re-run the spike to check.

**Pacing caveat:** 44 requests/min sustained for 89 s drew no complaint, which is
looser than the documented ~60 per 10 minutes. The documented figure should still
be treated as the design constraint for the recorder; one 89-second burst is not
evidence about sustained load over a full session.

### Two API details that cost time and will again

- `Contract.strike` is initialised to an UNSET sentinel. Writing a real `0.0` to
  list a whole chain turns it into a filter matching nothing, and TWS answers
  error 200. Leave it unset instead.
- Error **162 is overloaded**: it carries both "pacing violation" and "HMDS query
  returned no data". Branch on the message text, not the code, or an empty
  pre-market answer reads as throttling.
- `EWrapper.error` gained a leading `errorTime` argument in TWS API 10.30.
  `ibkr_adapter` locates `errorCode` as the int before the message string so both
  layouts work.

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
