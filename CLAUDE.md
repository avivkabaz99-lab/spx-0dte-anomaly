# spx-0dte-anomaly — project conventions

Read `HANDOFF.md` first (current state + next action), then `SPEC.md` (design),
then `DECISIONS.md` (why). This file is only the rules that apply on every turn.

## Hard rules
- **IBKR is read-only.** `BROKER_MODE=null`, `risk_state.kill_switch=true`. Never
  place an order, never implement `IBKRBroker`, without an explicit user decision.
- **Raw OPRA quotes never leave the machine.** No `bid`/`ask`/`mid` in `web/`, in a
  public view, in a committed file, or in any `anon` grant. `tests/test_rls.py` is
  the enforcement; if it needs changing, stop and ask.
- **Never read, print, or echo `.env`.** Report key *presence/shape* only
  (name, length, prefix). The deny rule blocks it; do not route around it.
- **Public repo.** Scan for key-shaped strings before every commit.

## Code
- Python 3.12, `uv`. Run everything as `.venv/bin/python` / `.venv/bin/pytest`.
- `ibapi` (official IBKR client, 10.45) is installed by `scripts/install_ibapi.sh`,
  never from PyPI, never vendored. Only `ingest/` and `execution/` may import it.
- Callback-based `ibapi` → a thin adapter in `ingest/ibkr_adapter.py` converts
  callbacks to plain dataclasses; everything downstream is synchronous pandas.
- Models: LightGBM first. No deep learning without a baseline to beat.
- Every backtest result reports optimistic / realistic / pessimistic execution.

## Database
- Schema lives in `supabase/migrations/`. Change it with a new numbered migration,
  never by editing the dashboard. Apply with `supabase db push` (linked to
  `rpcrwskakgkdivgjxvbv`).
- Raw snapshots → Parquet under `data/` (gitignored). Postgres holds derived data only.

## Workflow
- Append decisions to `DECISIONS.md` with why + what would reverse them.
- Update `HANDOFF.md` §0 at the end of every session.
- Conventional Commits. Don't push `feat/*` branches without asking.
