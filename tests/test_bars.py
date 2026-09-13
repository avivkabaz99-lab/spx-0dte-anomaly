"""The shared bars table: validation, on-disk layout, and the loader's precedence."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pyarrow.parquet as pq
import pytest

from spx0dte.features.bars import dates_on_disk, load_bars, validate_bars, write_bars
from spx0dte.features.session import session_grid


def bars(symbol: str = "SPX", day: date = date(2026, 9, 9), level: float = 6500.0) -> pd.DataFrame:
    ts = session_grid(day)
    return pd.DataFrame({
        "ts": ts,
        "symbol": symbol,
        "open": level,
        "high": level + 1,
        "low": level - 1,
        "close": level,
        "volume": 0.0,
    })


def test_validate_returns_sorted_utc_microsecond_copy() -> None:
    frame = pd.concat([bars("VIX1D"), bars("SPX")]).iloc[::-1]
    out = validate_bars(frame)
    assert list(out["symbol"].unique()) == ["SPX", "VIX1D"]
    assert out["ts"].is_monotonic_increasing is False  # sorted by (symbol, ts), not ts alone
    assert str(out["ts"].dtype) == "datetime64[us, UTC]"
    assert out.index.equals(pd.RangeIndex(len(out)))


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda f: f.assign(close=f["close"].mask(f.index == 3)), "NaN"),
        (lambda f: pd.concat([f, f.iloc[:1]]), "duplicate"),
        (lambda f: f.assign(ts=f["ts"].dt.tz_localize(None)), "tz-aware"),
        (lambda f: f.drop(columns="volume"), "missing"),
        (lambda f: f.assign(extra=1), "extra"),
        (lambda f: f.iloc[:0], "empty"),
    ],
)
def test_validate_rejects_defects(mutate, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_bars(mutate(bars()))


def test_write_partitions_by_symbol_and_eastern_date(tmp_path) -> None:
    frame = pd.concat([bars("SPX", date(2026, 9, 9)), bars("SPX", date(2026, 9, 10)),
                       bars("VIX1D", date(2026, 9, 9))])
    paths = write_bars(frame, tmp_path, now=datetime(2026, 9, 10, 20, 5, 7, tzinfo=UTC))
    rel = sorted(str(p.relative_to(tmp_path)) for p in paths)
    assert rel == [
        "symbol=SPX/date=2026-09-09/part-160507.parquet",
        "symbol=SPX/date=2026-09-10/part-160507.parquet",
        "symbol=VIX1D/date=2026-09-09/part-160507.parquet",
    ]
    meta = pq.read_metadata(paths[0])
    assert meta.num_rows == 390
    assert meta.row_group(0).column(0).compression == "ZSTD"
    assert dates_on_disk(tmp_path, "SPX") == {date(2026, 9, 9), date(2026, 9, 10)}


def test_load_prunes_by_date_and_symbol_and_keeps_last_part(tmp_path) -> None:
    write_bars(pd.concat([bars("SPX", date(2026, 9, 8)), bars("SPX", date(2026, 9, 9)),
                          bars("VIX", date(2026, 9, 9))]), tmp_path,
               now=datetime(2026, 9, 9, 21, 0, 0, tzinfo=UTC))
    # A later rewrite of 09-09 with a different level must win.
    write_bars(bars("SPX", date(2026, 9, 9), level=7000.0), tmp_path,
               now=datetime(2026, 9, 9, 22, 0, 0, tzinfo=UTC))

    out = load_bars(tmp_path, symbols=["SPX"], start=date(2026, 9, 9))
    assert out["symbol"].unique().tolist() == ["SPX"]
    assert len(out) == 390
    assert (out["close"] == 7000.0).all()
    assert str(out["ts"].dtype) == "datetime64[us, UTC]"

    everything = load_bars(tmp_path)
    assert len(everything) == 3 * 390


def test_load_raises_when_nothing_matches(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        load_bars(tmp_path)
