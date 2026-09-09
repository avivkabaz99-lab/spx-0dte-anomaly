# spx-0dte-anomaly

Research system for finding mispricing in same-day-expiry (0DTE) SPX options.

Two models, neither of which predicts an option's price — that only relearns
Black-Scholes. Both predict a **residual**:

- **Module B — variance risk premium.** Forecasts realized vol over the remaining
  session against implied (VIX1D).
- **Module A — IV surface residual.** Fits an SVI smile to the chain and models
  whether each strike's deviation mean-reverts.

Hard arbitrage checks (put-call parity, butterfly, box) are included as a
**data-quality validator**, not a strategy.

> **Research only.** The IBKR connection is read-only, `BROKER_MODE` defaults to
> `null`, and the kill switch defaults to on. Nothing here is investment advice.

See **[SPEC.md](SPEC.md)** for the full design and **[DECISIONS.md](DECISIONS.md)**
for the decision log.

## Market data

Raw option quotes are OPRA-licensed and are **never** committed or served
publicly. `data/` and `*.parquet` are gitignored; the `anon` database role has no
grant on the raw quote columns. `tests/test_rls.py` fails if that regresses.

## Setup

```bash
uv venv --python 3.12
uv pip install -e ".[dev,model]"
cp .env.example .env      # then fill values from your password manager
pytest
```

Apply the database schema:

```bash
supabase link --project-ref <ref>
supabase db push
```

## Status

Step 0 of 7 — scaffold and schema. See the roadmap in SPEC.md.
