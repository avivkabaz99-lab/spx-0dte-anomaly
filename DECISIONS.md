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

---

## 2026-09-09 — Live market data for the 0DTE chain, measured (sizes the recorder)

Measured with `scripts/spike_marketdata.py` against **IB Gateway on 4001, a live
account, read-only**, at ~06:00 ET during SPX Global Trading Hours. The adapter
subscribes, samples and cancels; it never places an order.

| Question | Measured |
|---|---|
| Are SPXW 0DTE quotes live or delayed? | **Live.** `delayed 0%` across 484 subscriptions; ATM 7675C quoted 8.20 x9 / 8.40 x36, iv 0.1593, delta 0.309 |
| Concurrent market-data lines | **≥ 484, no cap reached.** Error 101 (`max tickers`) never arrived; 484 is the entire chain (242 strikes × 2 rights) |
| Field coverage over the full chain | two-sided **69%**, iv **99%**, greeks **99%**, OI **97%**, volume **100%** |
| `undPrice` on the greeks | **Never populated — 0%** |
| Streaming SPX index quote | **Not entitled.** Error 354 on the `IND` contract; delayed is offered, live is not |

**What this decides for the recorder (step 2):**

1. **Record the whole chain, not an ATM window.** The line budget was the reason
   to narrow it and that reason is gone. 484 lines cost 484 `reqMktData` calls at
   startup and then zero requests: ticks stream, and sampling reads local state,
   so the historical pacing limit does not apply to the recorder at all.
2. **Spot has to be derived, not read.** The index is not entitled for streaming
   and `undPrice` never arrives, yet `reqHistoricalData` on the same `IND`
   contract does return bars (that is how the spike finds ATM). So spot comes
   from a 1-minute historical index poll — 1 request/min against a documented
   ~6/min budget — with put-call parity at the money as the cross-check.
3. **Missing is not zero.** 31% of the chain is one-sided at any moment. `Quote`
   keeps every unset field as `None` and the recorder writes null, so a wing with
   no bid never reads as a bid of 0.00.

**Two TWS behaviours worth not rediscovering:**

- Error **10090** ("part of requested market data is not subscribed,
  subscription-independent ticks are still active") arrives on *every* option
  subscription and is caused by the missing index entitlement. It is a notice,
  not a failure: bid, ask, iv, greeks and volume all stream normally afterwards.
  `MARKET_DATA_NOTICE_CODES` in the adapter keeps it off the WARNING channel and
  off the "this subscription failed" path — the first version of the spike
  counted it as an error and reported `0 receiving data` while 90% of the chain
  was quoting.
- The strike grid is **not uniform**: 5 points near the money widening to 25 in
  the wings. A synthetic ladder burns lines on contracts that do not exist
  (error 200, 134 of them in the second run). Build the ladder from
  `reqContractDetails`.

**What would reverse this:** losing the OPRA subscription, or an account change
that lowers the line budget below the chain size. The recorder should therefore
treat error 101 as a real condition and fall back to an ATM window rather than
assume 484 lines forever.

---

## 2026-09-09 — The recorder samples streamed state, it does not request quotes

**Decided:** `ingest/recorder.py` subscribes the entire SPXW 0DTE chain once
(484 lines), then samples the adapter's local ticker state every 30 seconds and
appends Parquet parts under `data/chains/date=YYYY-MM-DD/`.

**Why 30 seconds:** ~392k rows and ~21 MB per session, measured from a live
smoke run. One minute misses intraday 0DTE moves; fifteen seconds doubles the
volume for detail that the quote itself does not refresh at.

**Why sampling is free:** `reqMktData` streams. A sample is a dict read, not a
request, so the ~60 requests / 10 min historical limit constrains only the spot
poll (1 request / 45 s) and the backfill path.

**Why part files:** one file per flush (10 samples, 5 minutes) means a crash
costs minutes, and the directory is append-only — no rewrite, no lock.

**The line-budget fallback is the subscription order.** Contracts are opened
nearest-the-money first; anything TWS refuses with error 101 is dropped. If the
measured 484-line budget ever shrinks, what is lost is the far wing rather than
the strikes that matter, with no window logic to maintain.

**Two bugs the live smoke run caught that the unit tests did not:**

- Subscribing with a conId-only contract records a chain of **blank strikes**.
  TWS resolves on `conId` and ignores the rest, but the adapter reads strike,
  right and expiry back off the contract *it was handed*. `contract_from()` now
  copies every resolved field. The regression test builds its fake quote from
  the passed contract, the way the adapter does, so this cannot pass again.
- Error **10090** was written to `error_code` on all 3,872 rows. It is a
  session-wide entitlement notice, so it is now stored as null and a real
  per-contract error stays visible.

**Spot before the open is legitimately stale.** The SPX index does not print
during Global Trading Hours, so the poll returns the previous 15:59 ET close.
That is recorded as `spot` with its true `spot_ts`, never as a fresh value, and
the recorder warns when the age exceeds five minutes.

**Reverses if:** the line budget drops below the chain, or a spot source with
GTH coverage is added (ES futures would be the candidate).

---

## 2026-09-09 — The SPX index feed is delayed ~16 min, and the spot poll asked for a 60-second window

**Corrects the previous entry.** "The index does not print during Global Trading
Hours" was the wrong diagnosis for a stale `spot`. The index prints fine. This
account is not entitled to it in real time, so `reqHistoricalData` serves it
**delayed by about 16 minutes**, and that delay — not the trading session — is
what decides whether a request returns anything.

Measured against IB Gateway on 4001, a live account, 09:42 and 09:56 ET:

| Request | 09:42 ET (12 min after the open) | 09:56 ET (26 min after) |
|---|---|---|
| `60 S` / `1 min` | error 162, no data | never tested; cannot work |
| `1 D` / `1 min` | error 162, no data | 12 bars, newest 16 min old |
| `1 D` / `5–30 mins`, `1 hour` | error 162, no data | 1–3 bars, 16–27 min old |
| `2 D` / `1 min` | not tested | **402 bars, newest 16 min old** |
| `2 D` / `1 hour` | 7 bars, yesterday's close | — |

The rule the table shows: **a request answers only if its window reaches back
further than the feed's delay.** `60 S` never can, which is why `SpotPoll` had
failed on its very first call and the recorder exited with "no SPX print
available" before subscribing to anything. `1 D` is empty for roughly the first
20 minutes of every session for the same reason — it is scoped to a day the
delayed feed has not reached yet — which is why the pre-open smoke run that
built this code never hit the bug.

Two intermediate conclusions were drawn and discarded on the way, both from
testing at 09:42 only: that 1-minute bars were not entitled, and that the
account got no intraday index data at all. Neither is true.

**Decision:** `SPOT_DURATION = "2 D"`, `SPOT_BAR_SIZE = "1 min"`, `use_rth=True`.
Two days always spans the delay, the overnight and the weekend; 1 min is the
finest bar served. `SPOT_MAX_AGE` 60 s (one request a minute, far inside the
~60/10 min limit) and `SPOT_STALE_AFTER` 1800 s, above the normal ~16 min lag so
the warning means the feed actually stopped.

**`spot` is a delayed column and is never to be used as the spot at quote time.**
It orders the chain around the money, which tolerates a 16-minute-old centre.
Anything that needs the underlying *at the timestamp of a quote* — moneyness,
the SVI fit, every Module A residual — must recover it from **put-call parity**
on the recorded chain: both rights are stored at all 242 strikes, live and
entitled, so the forward is derivable per sample at no extra data cost.

**Reverses if:** the account gains a real-time index entitlement (then poll
`1 D`/`1 min` and drop the parity step), or a GTH spot source is added for the
pre-open hole, where the poll still legitimately returns the previous close.

## 2026-09-09 — First live session recorded, and the chain is clean

`data/chains/date=2026-09-09/`, sampled every 30 s. Coverage over the first 22
samples, checked with a script that prints only counts and percentages so no
quote value leaves the machine:

| | |
|---|---|
| Contracts per sample | 484, all of expiry 20260909 |
| Strikes | 242, 3200–10000, step 5, calls and puts exactly balanced |
| `ask` / `volume` / `spot` | 100% |
| `iv` and all four greeks | 99.5% |
| `open_interest` | 95.2% |
| `bid` / two-sided | 70.3% — the far wings have no bid, stored **null, never 0.0** |
| `delayed` rows | 0 — the options are live, only the index is not |
| Literal `0.0` bid/ask · crossed books · duplicate `(ts, con_id)` | 0 · 0 · 0 |
| `error_code` values present | none |
| Quote age | median 3 s, p95 28 s, max 49 s |
| Sample gap | 20 s median and 20 s max — no drift |

**Reverses if:** a later session shows two-sided coverage collapsing or a
non-empty `error_code`, either of which would mean an entitlement changed.

## 2026-09-09 — The recorder watched the wrong connection and wrote 8 minutes of frozen quotes

**Found by fault injection: the operator pulled this machine's network at 10:19
ET while the recorder was running.** That is the cheapest reproduction there is,
and it should be repeated against any future change to the sampling loop.

TWS logged the farms dropping (`TWS 2103` on `usopt`, `usfarm`, `usfarm.nj`,
`ushmds`), then `TWS 1100` for lost connectivity, then `1102` restoring it with
all farms back once the network returned.

**Through the whole outage the recorder kept sampling.** Its guard is
`self._api.is_connected`, which watches the **socket to TWS** — and IB Gateway
runs on this same machine, so that socket is a loopback connection that a
network cut cannot disturb. It stayed up. What broke was the link from **TWS out
to IBKR**, one layer above anything the guard can see. So the loop wrote the
last tick of every contract over and over: same schema, no `error_code`,
`delayed` false, indistinguishable from live data by every field except one.

This is not a hypothetical about IBKR's uptime. Any home network blip, VPN
switch or Wi-Fi handover produces exactly this, silently.

The signature is unmistakable in `quote_ts`, per-sample median quote age:

    14:19:28Z  36s     14:21:58Z  186s     14:24:28Z  336s
    14:19:58Z  66s     14:22:28Z  216s     14:24:58Z  366s
    14:20:28Z  96s     14:22:58Z  246s     14:25:28Z  396s
    14:20:58Z 126s     14:23:28Z  276s     14:25:58Z  426s
    14:21:28Z 156s     14:23:58Z  306s     14:26:28Z  456s

Age climbs by exactly the 30 s sample interval every sample: not one new quote
arrived. **14 samples, 6,776 rows, 15.1% of the session.** It is not confined to
illiquid strikes — ATM and the far wings froze together, which is what separates
an outage from thin quoting.

**The data is recoverable.** `quote_ts` is written per row, so the stretch is
exactly identifiable and filterable after the fact; nothing has to be thrown
away blind. Recording the tick's own timestamp instead of trusting the sample
timestamp is what saved the session.

**Decision:** `is_connected` is necessary but not sufficient. The adapter must
also track farm state from `1100` / `1102` / `2103` / `2105`, and the recorder
must stop writing while the farms are down rather than repeat a frozen book.
`1102` says "data maintained" and recovers on its own, so the loop should pause
and resume, not exit — exiting would hand a self-healing outage to launchd and
cost a full re-subscription.

**Until that ships, every session must be screened with
`scripts/verify_chain.py`**, and a per-sample median quote age above roughly the
sample interval means an outage, not slow quoting.

**Reverses if:** IBKR exposes a single connectivity signal that already covers
both layers, making the farm bookkeeping redundant.
