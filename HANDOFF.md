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

## 1. Next action — roadmap step 1: the IBKR spike

Half a day. Two assumptions in `DECISIONS.md` are unverified and order the whole
roadmap. Measure them with `ibapi` against TWS **paper (7497)**, read-only:

- Can `reqHistoricalData` return anything for a **live** SPXW 0DTE contract intraday?
- Can it return anything for **yesterday's expired** SPXW contract
  (`includeExpired=True`)? Expected: no, for index options.
- What do pacing limits look like across ~50 strikes?

Write the result into `DECISIONS.md` under the OPEN item. If expired contracts
*are* retrievable, the B-before-A ordering may flip — re-read SPEC §3.

Then step 2: `ingest/recorder.py` on a cron, because every trading day without
it is a day missing from Module A's dataset.

## 2. Things a new session will not guess

- `supabase db push` needs **no DB password** — the CLI mints a login role from the
  access token. Only the pytest connection needs `SUPABASE_DB_URL`.
- The Supabase project is in **us-east-2**, not eu-central-1 as recommended. Fine.
- `secret_scanning_non_provider_patterns` would not enable via API (org/plan limit).
- `RiskManager` is deliberately not written yet — lands with `PaperBroker` (step 5).
- The user's global CLAUDE.md says `ib_insync`; this project overrides to `ibapi`.
- Language: the user works in English but asks for explanations in Hebrew
  ("הסבר" / "בעברית"). Technical terms stay English either way.
