"""CBOE daily index history: VIX1D, VIX9D, VIX from the public CSV endpoint.

This is the fallback for accounts whose IBKR entitlement does not serve the
CBOE volatility indices intraday. Daily prints are turned into two bars per
session -- the open at 09:31 ET and the close at 15:59 ET -- so the as-of
join in the feature code stays honest: the open is known a minute after the
open, the close only at the close. Intraday change features degenerate to
per-day constants on this source, which is why the IBKR path is preferred.

Usage:
    .venv/bin/python -m spx0dte.ingest.cboe --symbols VIX1D,VIX9D,VIX --out data/cboe
"""

from __future__ import annotations

import argparse
import io
import logging
from collections.abc import Sequence
from datetime import datetime, time
from pathlib import Path

import httpx
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from spx0dte.features.bars import COLUMNS, validate_bars
from spx0dte.features.session import EASTERN

logger = logging.getLogger(__name__)

CBOE_DAILY_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{symbol}_History.csv"
DEFAULT_SYMBOLS = ("VIX1D", "VIX9D", "VIX")

DAILY_SCHEMA = pa.schema([
    ("date", pa.date32()),
    ("symbol", pa.string()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
])

# Bar starts for the two synthetic prints. 09:31 so the open is "known" at
# 09:32, after the first real bar; 15:59 so the close is known at 16:00.
OPEN_PRINT = time(9, 31)
CLOSE_PRINT = time(15, 59)


def parse_daily(text: str, symbol: str) -> pd.DataFrame:
    """Parse CBOE's `DATE,OPEN,HIGH,LOW,CLOSE` CSV (dates `MM/DD/YYYY`)."""
    frame = pd.read_csv(io.StringIO(text))
    expected = ["DATE", "OPEN", "HIGH", "LOW", "CLOSE"]
    if list(frame.columns) != expected:
        raise ValueError(f"unexpected CBOE columns for {symbol}: {list(frame.columns)}")
    out = pd.DataFrame({
        "date": pd.to_datetime(frame["DATE"], format="%m/%d/%Y").dt.date,
        "symbol": symbol,
        "open": frame["OPEN"].astype(float),
        "high": frame["HIGH"].astype(float),
        "low": frame["LOW"].astype(float),
        "close": frame["CLOSE"].astype(float),
    })
    if out.isna().any().any():
        raise ValueError(f"CBOE {symbol} history has missing values")
    return out.sort_values("date").reset_index(drop=True)


def fetch_daily(symbol: str, client: httpx.Client) -> pd.DataFrame:
    url = CBOE_DAILY_URL.format(symbol=symbol)
    response = client.get(url, timeout=30.0)
    response.raise_for_status()
    frame = parse_daily(response.text, symbol)
    logger.info("%s: %d daily rows, %s..%s", symbol, len(frame),
                frame["date"].iloc[0], frame["date"].iloc[-1])
    return frame


def write_daily(frame: pd.DataFrame, root: Path) -> Path:
    """One file per symbol, overwritten on each fetch (the history is small)."""
    symbols = frame["symbol"].unique()
    if len(symbols) != 1:
        raise ValueError(f"write one symbol at a time, got {symbols}")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{symbols[0]}.parquet"
    table = pa.Table.from_pandas(frame, schema=DAILY_SCHEMA, preserve_index=False)
    pq.write_table(table, path, compression="zstd")
    return path


def load_daily(root: Path, symbols: Sequence[str] = DEFAULT_SYMBOLS) -> pd.DataFrame:
    frames = []
    for symbol in symbols:
        path = root / f"{symbol}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"no CBOE history for {symbol} at {path}")
        frames.append(pq.read_table(path, schema=DAILY_SCHEMA).to_pandas())
    return pd.concat(frames, ignore_index=True)


def daily_to_bars(daily: pd.DataFrame) -> pd.DataFrame:
    """Two bars per session per symbol: the open print and the close print."""
    rows = []
    for print_time, column in ((OPEN_PRINT, "open"), (CLOSE_PRINT, "close")):
        ts = pd.to_datetime([
            datetime.combine(day, print_time, tzinfo=EASTERN) for day in daily["date"]
        ], utc=True).as_unit("us")
        value = daily[column].to_numpy()
        rows.append(pd.DataFrame({
            "ts": ts,
            "symbol": daily["symbol"].to_numpy(),
            "open": value,
            "high": value,
            "low": value,
            "close": value,
            "volume": 0.0,
        }))
    return validate_bars(pd.concat(rows, ignore_index=True)[list(COLUMNS)])


def main() -> int:
    parser = argparse.ArgumentParser(description="Download CBOE daily index history.")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--out", type=Path, default=Path("data/cboe"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    with httpx.Client() as client:
        for symbol in args.symbols.split(","):
            path = write_daily(fetch_daily(symbol.strip(), client), args.out)
            logger.info("wrote %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
