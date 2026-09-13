# HANDOFF

## 0. Where we are (updated 2026-09-13)

**Roadmap steps 0–2c are done; step 3 (Module B) runs end to end on synthetic
data. Suite: 141 passed.**

| Done | Verified |
|---|---|
| Public repo `avivkabaz99-lab/spx-0dte-anomaly`, secret scanning + push protection on | OK |
| Supabase project `spx-0dte-anomaly` (`rpcrwskakgkdivgjxvbv`, us-east-2), linked | OK |
| 3 migrations pushed, remote == local | OK `supabase migration list` |
| `.venv` with dev deps + `ibapi 10.45.1` | OK import check |
| `model` extra: `lightgbm 4.7`, `scikit-learn 1.9` | OK import check |
| Secrets-hygiene + RLS tests (the OPRA boundary) | OK |
| Recorder on launchd, farm-state guard (`1100`/`1102`/`2103`/`2105`) | OK, see `DECISIONS.md` |
| Sessions recorded: **2026-09-09, 2026-09-10** | OK — 09-10 ends at 15:56 ET, see §1 |
| Shared 1-min bars schema (`features/bars.py`) + synthetic generator with a known premium | OK |
| Module B features/target with enforced past/future split | OK, leakage tests |
| Module B LightGBM trainer, date split, two baselines | OK on synthetic, see below |
| Supabase `model_runs` provenance client (`db/client.py`) | OK, pure row builder tested |
| CBOE daily VIX / VIX1D / VIX9D history downloaded to `data/cboe/` | OK |
| IBKR paced backfill of 1-min index bars (`ingest/index_history.py`) | **Written and tested, never run live** |

**Module B on 200 synthetic sessions** (`data/models/module_b/20260913T164635Z/`):
test MSE 0.0044 against 0.0202 (zero baseline) and 0.0082 (train mean),
directional accuracy 0.92. Top gain importance: `vix1d`, `ts_1d_9d`,
`overnight_gap`, `minutes_to_close`. The no-premium control set beats zero (the
Jensen offset in log RV) and does not beat the mean — that is the leakage guard,
and it must stay that way after any feature change.

**LightGBM needs Homebrew `libomp` on this Mac.** `uv pip install lightgbm` alone
gives an import-time `dlopen` failure on `@rpath/libomp.dylib`; `brew install
libomp` fixes it. It is a system package, so a fresh machine needs it again.

**`pandas 3.0.5` is what is installed**, while `pyproject.toml` only asks for
`>=2.2`. 3.0 changed copy-on-write and the default string dtype. The Module B
feature code is written against 3.0.

`.env` is filled. `.env.example` untouched, `git status` clean.

### Connecting to the database — settled, do not re-derive

The Session pooler host was verified against the live server by probing both
candidates with a dummy password: `aws-0` answered `password authentication
failed` (knows the tenant), `aws-1` answered `tenant/user not found`.

```
SUPABASE_DB_URL=postgresql://postgres.rpcrwskakgkdivgjxvbv:<pw>@aws-0-us-east-2.pooler.supabase.com:5432/postgres
```

Direct connection (`db.<ref>.supabase.co`) is **IPv6-only and unreachable** from
this machine. Use the pooler.

The DB password contains `@`, `/`, `%` and `&`, so it **must be percent-encoded**
inside the URI. Hand-editing it into the middle of the line breaks the URI every
time — those characters are the URI delimiters. If the password is ever rotated,
re-encode it with a script rather than by hand: paste the raw password into a
temporary `TEMP_DB_PASSWORD=` line, then build the URI with
`urllib.parse.quote(pw, safe='')`, write it to `SUPABASE_DB_URL`, and delete the
temporary line. The value is never printed or pasted into chat.

## 1. Next action — real bars into Module B, and keep the recorder fed

### Recording status: the 5-session count restarted

Step 2 asks for **five consecutive trading days, zero gaps**. Two are on disk:

- **2026-09-09** — full session, chain clean (numbers in `DECISIONS.md`), but
  15.1% of it is frozen quotes from a network cut; filter on `quote_ts` age.
  The farm-state guard that prevents this shipped afterwards (`6ecc3ad`).
- **2026-09-10** — 484 samples, clean through **15:56 ET**, then `TWS
  connection lost` four minutes before the close and the recorder stopped. The
  last part was flushed (1,976 rows). Not yet screened with
  `scripts/verify_chain.py`.
- **2026-09-11 (Friday) — not recorded.** The machine was not connected that
  day; launchd never got a chance to fire. Nothing to fix in the code, but the
  consecutive count starts again on the next session.

So the recorder needs the Mac **awake, online, with IB Gateway logged in on
4001** every weekday from 09:00 ET. Check before the open:

```bash
launchctl print "gui/$UID/com.spx0dte.recorder" | grep -E "state|runs|last exit"
tail -f logs/recorder-$(TZ=America/New_York date +%F).log
```

**Known cosmetic bug, not yet fixed:** when the recorder exits on a lost TWS
connection, `unsubscribe_all()` calls `ibapi.cancelMktData`, which compares
against `serverVersion()` — `None` after the disconnect — and raises
`TypeError: '<=' not supported between instances of 'int' and 'NoneType'`
(`recorder.py:465` → `ibkr_adapter.py:494`). The data is already flushed by
then, so nothing is lost; the exit code is just non-zero. Guard the unsubscribe
on `is_connected` or swallow the ibapi error there.

### Step 3 against real data, in order

1. **Run the backfill for the first time** — read-only, historical only, client
   id 12 beside the recorder's 11. Probe first to learn which indices this
   entitlement serves intraday, then fetch:

   ```bash
   IBKR_PORT=4001 .venv/bin/python -m spx0dte.ingest.index_history --probe
   IBKR_PORT=4001 .venv/bin/python -m spx0dte.ingest.index_history --days 126
   ```

   Output: `data/bars/symbol=SYM/date=YYYY-MM-DD/`. 2 D chunks, 10.5 s apart,
   so ~126 sessions of one symbol takes ~11 minutes. Rerunning fetches only
   missing dates. Error 162 is branched on its text; an unentitled symbol is
   skipped, not fatal.

2. **Train on real bars**, CBOE daily fills whatever IBKR does not serve
   (`--cboe` joins the 09:31 / 15:59 ET prints as-of, never a close before the
   close):

   ```bash
   .venv/bin/python -m spx0dte.models.module_b.train --bars data/bars --cboe data/cboe --record-run
   ```

   Step 3 is verified only if the test fold beats **both** baselines. If VIX1D
   comes only from CBOE daily, `vix1d` is constant intraday and `ts_1d_9d`
   loses most of its information — say so in `DECISIONS.md` rather than
   reading a weak result as "no premium".

3. **Screen 2026-09-10 with `scripts/verify_chain.py`** and add its coverage
   row to `DECISIONS.md`.

4. **Fix the `unsubscribe_all` TypeError** above.

Synthetic path, for reference and for the pipeline tests:

```bash
.venv/bin/python -m spx0dte.synth --days 200 --seed 0
.venv/bin/python -m spx0dte.synth --days 200 --no-premium --out data/synth/bars_nopremium
.venv/bin/python -m spx0dte.models.module_b.train --synthetic
```

### Recorder operations — unchanged

The launchd agent `com.spx0dte.recorder` fires `scripts/record_session.sh`
**every 30 minutes** on **port 4001, the live IB Gateway, read-only**. The
script starts a recorder on weekdays between 09:00 and 16:00 ET, only if one is
not already running; frequent firing doubles as crash restart. It is not
scheduled at the open on purpose: this machine runs on Israel time and the
local-to-Eastern offset moves between 6 and 8 hours across two countries' DST
changes. Exchange holidays are not listed anywhere: the chain resolves empty
and the recorder exits saying so. The port lives in the plist, never in the
repo.

```bash
./scripts/install_launchd.sh 4001      # reinstall, or change the port
launchctl bootout "gui/$UID/com.spx0dte.recorder"   # stop recording entirely
IBKR_PORT=4001 caffeinate -is .venv/bin/python -m spx0dte.ingest.recorder   # manual run
```

Output: `data/chains/date=YYYY-MM-DD/part-HHMMSS.parquet`, flushed every 5
minutes. `caffeinate` matters — a Mac that sleeps stops recording silently. The
recorder stops itself if TWS drops the connection (IB Gateway restarts daily)
and pauses, rather than exits, while the farms are down (`1100` → `1102`).

**`spot` is a delayed column** (~16 min, index feed). Good enough to centre the
chain, wrong for any feature that needs the underlying at quote time — recover
that from put-call parity on the recorded chain, which stores both rights at
all strikes.

Still open after step 3, in order:

1. **Step 2d, the chain backfill path.** Yesterday's expiry only — the window
   is one trading day. `reqHistoricalData` with `includeExpired=True`, BID/ASK/
   TRADES at 1 min; the ~60 requests / 10 min limit applies, so a full chain is
   not backfillable: pick an ATM window and pace it.
2. **A GTH spot source.** The index does not print outside regular hours, so
   pre-open rows carry the previous close with an honest `spot_ts`. ES futures
   would fix it if pre-open data ever matters.

Re-running the spikes:

```bash
IBKR_PORT=4001 .venv/bin/python scripts/spike_historical.py --pacing 65
IBKR_PORT=4001 .venv/bin/python scripts/spike_marketdata.py --lines 520
```

`.env` still holds `IBKR_PORT=7497` (TWS paper, currently not running). Port 4001
is the **live** IB Gateway and is passed per-run on purpose, so no committed
default points at a live account.

## 2. Things a new session will not guess

- `supabase db push` needs **no DB password** — the CLI mints a login role from the
  access token. Only the pytest connection needs `SUPABASE_DB_URL`.
- The Supabase project is in **us-east-2**, not eu-central-1 as recommended. Fine.
- `secret_scanning_non_provider_patterns` would not enable via API (org/plan limit).
- `RiskManager` is deliberately not written yet — lands with `PaperBroker` (step 5).
- The user's global CLAUDE.md says `ib_insync`; this project overrides to `ibapi`.
- `data/` is gitignored: bars, chains, CBOE history and model artifacts live
  only on this machine. Losing the Mac loses the recorded sessions.
- Language: the user works in English but asks for explanations in Hebrew
  ("הסבר" / "בעברית"). Technical terms stay English either way.
