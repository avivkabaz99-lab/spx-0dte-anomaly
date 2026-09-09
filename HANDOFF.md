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

**Step 1 and step 2a-2b are complete too.** The adapter now also streams market
data (`subscribe` / `quotes` / `unsubscribe`), and `scripts/spike_marketdata.py`
measured what this account actually receives. Suite: **55 passed**.

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

## 1. Next action — roadmap step 2c: `ingest/recorder.py`

Steps 1, 2a and 2b are done. What they settled, in one line each (full numbers in
`DECISIONS.md` under 2026-09-09):

- Expired SPXW chains are retrievable for **exactly one trading day**, so a
  missed session can be backfilled the next morning and only then.
- Live SPXW 0DTE quotes **are** entitled; the **whole 484-line chain** can be
  subscribed at once, so no ATM window is needed.
- The SPX index is **not** entitled for streaming and `undPrice` never arrives,
  so spot is derived: a 1-minute historical `IND` poll, cross-checked against
  put-call parity at the money.

Build `ingest/recorder.py`:

- Resolve today's SPXW chain with `reqContractDetails`, subscribe every strike
  and both rights, sample the local ticker state on a timer (no request per
  sample — streaming means pacing does not apply), and append to Parquet under
  `data/` (gitignored; raw quotes never leave the machine).
- Flush every few minutes so a crash costs minutes, not a session.
- Treat error **101** as a real condition and fall back to an ATM window; the
  484-line budget is measured, not guaranteed.
- Add a backfill path for **yesterday's** expiry via `reqHistoricalData` with
  `includeExpired=True`. That window is one day wide and is where the historical
  pacing limit (~60 requests / 10 min) does apply.

Running the spikes again:

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
