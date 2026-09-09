# HANDOFF

## 0. Where we are (updated 2026-09-09)

**Step 0 of the roadmap is complete. The full suite is green.**

| Done | Verified |
|---|---|
| Public repo `avivkabaz99-lab/spx-0dte-anomaly`, secret scanning + push protection on | OK |
| Supabase project `spx-0dte-anomaly` (`rpcrwskakgkdivgjxvbv`, us-east-2), linked | OK |
| 3 migrations pushed, remote == local | OK `supabase migration list` |
| `.venv` with dev deps + `ibapi 10.45.1` | OK import check |
| Secrets-hygiene tests | OK 12 passed |
| **RLS tests — the OPRA boundary** | **OK — 27 passed, 0 skipped** |

**Steps 1 and 2a-2c are complete too.** The adapter also streams market data
(`subscribe` / `quotes` / `unsubscribe`), `scripts/spike_marketdata.py` measured
what this account actually receives, and `ingest/recorder.py` writes the chain to
Parquet. Suite: **73 passed**.

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

## 1. Next action — start today's recording, then step 2d (backfill)

Steps 1, 2a, 2b and 2c are done. `ingest/recorder.py` records the full SPXW 0DTE
chain and was smoke-run live against IB Gateway: 484 contracts, 242 strikes,
C/P balanced, iv on 100% of rows, two-sided on 70%, ~392k rows and ~21 MB per
session at a 30-second interval. Suite: **73 passed**.

**It runs itself.** The launchd agent `com.spx0dte.recorder` is installed and
active on **port 4001, the live IB Gateway, read-only**:

```bash
launchctl print "gui/$UID/com.spx0dte.recorder" | grep -E "state|runs|last exit"
tail -f logs/recorder-$(TZ=America/New_York date +%F).log
./scripts/install_launchd.sh 4001      # reinstall, or change the port
launchctl bootout "gui/$UID/com.spx0dte.recorder"   # stop recording entirely
```

The agent fires `scripts/record_session.sh` **every 30 minutes** and the script
decides. It is not scheduled at the open on purpose: this machine runs on Israel
time and the local-to-Eastern offset moves between 6 and 8 hours across two
countries' DST changes, so a fixed local trigger drifts twice a year. Gating on
Eastern time inside the script is exact all year, and firing often doubles as a
crash restart — a recorder that dies mid-session is back within half an hour,
which is what "5 consecutive trading days, zero gaps" actually needs.

The script starts a recorder on weekdays between 09:00 and 16:00 ET, and only if
one is not already running. Exchange holidays are not listed anywhere: the chain
resolves empty and the recorder exits saying so. The port lives in the plist,
never in the repo, so nothing committed points at a live account.

Manual run, if the agent is off:

```bash
IBKR_PORT=4001 caffeinate -is .venv/bin/python -m spx0dte.ingest.recorder
```

Output: `data/chains/date=YYYY-MM-DD/part-HHMMSS.parquet`, flushed every 5
minutes. `caffeinate` matters — a Mac that sleeps stops recording silently. The
recorder stops itself if TWS drops the connection (IB Gateway restarts daily),
and the agent starts a fresh one on the next fire.

**Still open, in order:**

1. **Watch the first full session end to end.** Verification for roadmap step 2
   is five consecutive trading days with no gaps; nothing has been recorded for a
   whole session yet.
2. **Step 2d, the backfill path.** Yesterday's expiry only — the window is one
   trading day. `reqHistoricalData` with `includeExpired=True`, BID/ASK/TRADES
   at 1 min, and this is where the ~60 requests / 10 min limit does apply, so a
   full chain is not backfillable: pick an ATM window and pace it.
3. **A GTH spot source.** The index does not print outside regular hours, so
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
- Language: the user works in English but asks for explanations in Hebrew
  ("הסבר" / "בעברית"). Technical terms stay English either way.
