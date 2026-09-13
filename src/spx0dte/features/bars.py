"""The one bar table every Module B input is written as.

Both the IBKR backfill and the synthetic generator emit this shape, so the
feature code has a single reader and cannot tell the sources apart. Index
bars only: no OPRA field exists here, and the files may live anywhere.

Layout on disk mirrors the chain recorder, keyed by symbol and Eastern date:

    <root>/symbol=SPX/date=2026-09-09/part-HHMMSS.parquet

One part per (symbol, date) per write. A re-run of the same date writes a
later part, and the loader keeps the later one.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from spx0dte.features.session import EASTERN, et_date

logger = logging.getLogger(__name__)

BARS_SCHEMA = pa.schema([
    ("ts", pa.timestamp("us", tz="UTC")),  # bar START, one minute wide, RTH only
    ("symbol", pa.string()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),  # 0.0 for indices, never null
])

COLUMNS = tuple(BARS_SCHEMA.names)
KEY = ["symbol", "ts"]


def validate_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a sorted, de-duplicated copy or raise `ValueError` describing the defect."""
    missing = set(COLUMNS) - set(frame.columns)
    extra = set(frame.columns) - set(COLUMNS)
    if missing or extra:
        raise ValueError(f"bars columns: missing={sorted(missing)} extra={sorted(extra)}")
    if frame.empty:
        raise ValueError("bars frame is empty")
    ts = frame["ts"]
    if not isinstance(ts.dtype, pd.DatetimeTZDtype):
        raise ValueError(f"ts must be tz-aware, got {ts.dtype}")
    if frame[list(COLUMNS)].isna().any().any():
        bad = frame.columns[frame.isna().any()].tolist()
        raise ValueError(f"bars contain NaN in {bad}")
    if frame.duplicated(KEY).any():
        n = int(frame.duplicated(KEY).sum())
        raise ValueError(f"bars contain {n} duplicate (symbol, ts) rows")

    out = frame[list(COLUMNS)].sort_values(KEY, kind="stable").reset_index(drop=True)
    out["ts"] = out["ts"].dt.tz_convert("UTC").astype("datetime64[us, UTC]")
    out["symbol"] = out["symbol"].astype("str")
    return out


def write_bars(frame: pd.DataFrame, root: Path, now: datetime | None = None) -> list[Path]:
    """Write one part file per (symbol, Eastern date). Returns the paths written."""
    frame = validate_bars(frame)
    stamp = (now or datetime.now(UTC)).astimezone(EASTERN)
    written: list[Path] = []
    for (symbol, day), group in frame.groupby([frame["symbol"], et_date(frame["ts"])]):
        directory = root / f"symbol={symbol}" / f"date={day:%Y-%m-%d}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"part-{stamp:%H%M%S}.parquet"
        table = pa.Table.from_pandas(group, schema=BARS_SCHEMA, preserve_index=False)
        pq.write_table(table, path, compression="zstd")
        written.append(path)
    logger.info("wrote %d rows to %d part files under %s", len(frame), len(written), root)
    return written


def _partitions(
    root: Path,
    symbols: Sequence[str] | None,
    start: date | None,
    end: date | None,
) -> list[Path]:
    wanted = set(symbols) if symbols is not None else None
    paths: list[Path] = []
    for symbol_dir in sorted(root.glob("symbol=*")):
        symbol = symbol_dir.name.split("=", 1)[1]
        if wanted is not None and symbol not in wanted:
            continue
        for date_dir in sorted(symbol_dir.glob("date=*")):
            day = date.fromisoformat(date_dir.name.split("=", 1)[1])
            if (start is not None and day < start) or (end is not None and day > end):
                continue
            paths.extend(sorted(date_dir.glob("part-*.parquet")))
    return paths


def dates_on_disk(root: Path, symbol: str) -> set[date]:
    """Eastern dates that already have at least one part file for `symbol`."""
    return {
        date.fromisoformat(p.name.split("=", 1)[1])
        for p in (root / f"symbol={symbol}").glob("date=*")
        if any(p.glob("part-*.parquet"))
    }


def load_bars(
    root: Path,
    symbols: Sequence[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """Read every part under `root`, pruning by directory name, later parts winning."""
    paths = _partitions(root, symbols, start, end)
    if not paths:
        raise FileNotFoundError(f"no bars under {root} for symbols={symbols} {start}..{end}")
    frames = [pq.read_table(p, schema=BARS_SCHEMA).to_pandas() for p in paths]
    frame = pd.concat(frames, ignore_index=True)
    frame = frame.drop_duplicates(KEY, keep="last")
    return validate_bars(frame)
