"""Coverage and integrity report for a recorded chain partition.

Roadmap step 2 asks for five consecutive trading days with no gaps, and "no
gaps" is not something a log line can prove: the recorder happily writes a
session in which the wings stopped quoting or the feed silently went delayed.
This script is the check that a day is actually usable.

It prints **no quote values**. Every figure is a count, a percentage, a
timestamp or a contract spec, so the output is safe to paste into a review or a
handoff without carrying raw OPRA quotes out of `data/`.

What the numbers should look like on a healthy session, measured 2026-09-09:

    contracts/sample  484        ask/volume  100%      iv, greeks  ~99.5%
    strikes           242        two-sided   ~70%      delayed     0
    literal 0.0 bid/ask, crossed books, duplicates, error codes:  all 0

`bid` at ~70% is correct, not missing data: the far wings have no bid, and a
one-sided quote is stored as null. A literal `0.0` there would be the bug.

Usage:
    .venv/bin/python scripts/verify_chain.py
    .venv/bin/python scripts/verify_chain.py data/chains/date=2026-09-09
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

# Columns whose null-rate says whether the session is usable.
COVERAGE = [
    "bid", "ask", "last", "volume", "open_interest",
    "iv", "delta", "gamma", "vega", "theta", "spot",
]


def load(root: Path) -> pd.DataFrame:
    """Concatenate every Parquet part under `root`."""
    parts = sorted(root.rglob("*.parquet"))
    if not parts:
        raise SystemExit(f"no parquet under {root}")
    frame = pd.concat((pd.read_parquet(p) for p in parts), ignore_index=True)
    print(f"parts            {len(parts)}")
    return frame


def report(df: pd.DataFrame) -> None:
    """Print the coverage and integrity block for one partition."""
    samples = df["ts"].nunique()
    step = df["strike"].drop_duplicates().sort_values().diff().mode().iloc[0]

    print(f"rows             {len(df):,}")
    print(f"samples (ts)     {samples}")
    print(f"contracts/sample {len(df) / samples:.0f}")
    print(f"window           {df['ts'].min():%H:%M:%S} .. {df['ts'].max():%H:%M:%S} UTC")
    print(f"expiries         {sorted(df['expiry'].unique())}")
    print(f"strikes          {df['strike'].nunique()}  "
          f"range {df['strike'].min():.0f}..{df['strike'].max():.0f}  step {step:.0f}")
    print(f"C/P split        {(df['right'] == 'C').sum():,} / {(df['right'] == 'P').sum():,}")

    print("\nfield coverage (share of rows that are non-null)")
    for col in COVERAGE:
        print(f"  {col:15} {100 * df[col].notna().mean():5.1f}%")
    two_sided = 100 * (df["bid"].notna() & df["ask"].notna()).mean()
    print(f"  {'two-sided':15} {two_sided:5.1f}%")

    print("\nintegrity")
    zero = (df[["bid", "ask"]] == 0.0).any(axis=1).sum()
    print(f"  rows with a literal 0.0 bid/ask   {zero:,}   (missing must be null, not 0.0)")
    print(f"  crossed book (bid > ask)          {(df['bid'] > df['ask']).sum():,}")
    print(f"  duplicate (ts, con_id)            {df.duplicated(['ts', 'con_id']).sum():,}")
    print(f"  distinct error_code               {sorted(df['error_code'].dropna().unique())}")
    print(f"  delayed rows                      {int(df['delayed'].sum()):,}")

    quote_age = (df["ts"] - df["quote_ts"]).dt.total_seconds()
    print(f"  quote age  median {quote_age.median():.0f}s  "
          f"p95 {quote_age.quantile(0.95):.0f}s  max {quote_age.max():.0f}s")
    # spot rides a ~16 min delayed index feed by design; see DECISIONS.md.
    spot_age = (df["ts"] - df["spot_ts"]).dt.total_seconds() / 60
    print(f"  spot age   median {spot_age.median():.0f} min  (delayed index feed, by design)")
    gaps = df["ts"].drop_duplicates().sort_values().diff().dt.total_seconds().dropna()
    print(f"  sample gap median {gaps.median():.0f}s  max {gaps.max():.0f}s")


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "data/chains")
    report(load(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
