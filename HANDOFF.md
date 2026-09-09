# HANDOFF

## 0. Where we are (updated 2026-09-09)

**Step 0 of the roadmap is complete except for one verification.**

| Done | Verified |
|---|---|
| Public repo `avivkabaz99-lab/spx-0dte-anomaly`, secret scanning + push protection on | ✅ |
| Supabase project `spx-0dte-anomaly` (`rpcrwskakgkdivgjxvbv`, us-east-2), linked | ✅ |
| 3 migrations pushed, remote == local | ✅ `supabase migration list` |
| `.venv` with dev deps + `ibapi 10.45.1` | ✅ import check |
| Secrets-hygiene tests | ✅ 12 passed |
| **RLS tests — the OPRA boundary** | ❌ **15 skipped** — `.env` not filled |

`.env` exists with `SUPABASE_URL` prefilled. The other three keys are **empty on
disk** as of the last check — the user edited in Cursor but the values did not
reach the file (unsaved, wrong tab, or wrong folder). Nothing leaked:
`.env.example` is untouched per `git status`.

## 1. Next action — exactly this, in order

1. User fills three values in `~/Developer/spx-0dte-anomaly/.env` **from the editor**:
   - `SUPABASE_ANON_KEY` — Dashboard → Project Settings → API Keys → legacy tab → `anon`
     (or `sb_publishable_…` from the new tab)
   - `SUPABASE_SERVICE_ROLE_KEY` — same screen → `service_role` → Reveal
     (or create a `sb_secret_…` key; shown once)
   - `SUPABASE_DB_URL` — top bar → Connect → URI → **Session pooler** (`:5432/`),
     `[YOUR-PASSWORD]` replaced
   Then **Cmd+S** and confirm the tab is named `.env`, not `.env.example`.

2. Verify shape without reading values:
   ```bash
   .venv/bin/python -c "
   import os; from dotenv import load_dotenv; load_dotenv('.env')
   for k in ('SUPABASE_ANON_KEY','SUPABASE_SERVICE_ROLE_KEY','SUPABASE_DB_URL'):
       v=os.environ.get(k,''); print(k, 'EMPTY' if not v else f'set ({len(v)} chars)')"
   ```
   Note `load_dotenv('.env')` with the explicit path — the bare call fails from stdin.

3. Run the suite:
   ```bash
   .venv/bin/pytest -q 2>&1 | sed -E 's#postgres(ql)?://[^[:space:]]+#postgresql://***#g'
   ```
   **Success = 27 passed, 0 skipped.** If any RLS test fails, the database is
   exposing something it must not; fix the migration, do not weaken the test.

## 2. After that — roadmap step 1: the IBKR spike

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

## 3. Things a new session will not guess

- `supabase db push` needs **no DB password** — the CLI mints a login role from the
  access token. Only the pytest connection needs `SUPABASE_DB_URL`.
- The Supabase project is in **us-east-2**, not eu-central-1 as recommended. Fine.
- `secret_scanning_non_provider_patterns` would not enable via API (org/plan limit).
- `RiskManager` is deliberately not written yet — lands with `PaperBroker` (step 5).
- The user's global CLAUDE.md says `ib_insync`; this project overrides to `ibapi`.
- Language: the user works in English but asks for explanations in Hebrew
  ("הסבר" / "בעברית"). Technical terms stay English either way.
