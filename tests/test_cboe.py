"""CBOE daily history: parsing, the two-prints-per-day expansion, and the fetch URL."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pandas as pd
import pytest

from spx0dte.ingest.cboe import (
    CBOE_DAILY_URL,
    daily_to_bars,
    fetch_daily,
    load_daily,
    parse_daily,
    write_daily,
)

FIXTURE = Path(__file__).parent / "fixtures" / "vix1d_history_sample.csv"


def test_parse_daily_reads_cboe_layout() -> None:
    frame = parse_daily(FIXTURE.read_text(), "VIX1D")
    assert list(frame.columns) == ["date", "symbol", "open", "high", "low", "close"]
    assert frame["date"].iloc[0] == date(2022, 5, 13)
    assert frame["date"].is_monotonic_increasing
    assert frame["close"].iloc[-1] == pytest.approx(12.98)
    assert (frame["symbol"] == "VIX1D").all()


def test_parse_daily_rejects_other_layouts() -> None:
    with pytest.raises(ValueError, match="columns"):
        parse_daily("a,b\n1,2\n", "VIX1D")


def test_daily_to_bars_places_open_and_close_prints_in_eastern_time() -> None:
    daily = parse_daily(FIXTURE.read_text(), "VIX1D")
    bars = daily_to_bars(daily)
    assert len(bars) == 2 * len(daily)
    # 2026-09-11 is EDT (UTC-4): 09:31 ET = 13:31Z, 15:59 ET = 19:59Z.
    sept = bars[bars["ts"].dt.date == date(2026, 9, 11)].set_index("ts")
    assert sept.index.tolist() == [
        pd.Timestamp("2026-09-11 13:31", tz="UTC"), pd.Timestamp("2026-09-11 19:59", tz="UTC")
    ]
    assert sept["close"].tolist() == [pytest.approx(9.54), pytest.approx(12.98)]
    assert (bars["volume"] == 0.0).all()


def test_daily_to_bars_in_winter_is_est() -> None:
    daily = pd.DataFrame({"date": [date(2026, 1, 15)], "symbol": "VIX", "open": [15.0],
                          "high": [16.0], "low": [14.0], "close": [15.5]})
    bars = daily_to_bars(daily)
    assert bars["ts"].tolist() == [
        pd.Timestamp("2026-01-15 14:31", tz="UTC"), pd.Timestamp("2026-01-15 20:59", tz="UTC")
    ]


def test_write_and_load_round_trip(tmp_path) -> None:
    daily = parse_daily(FIXTURE.read_text(), "VIX1D")
    path = write_daily(daily, tmp_path)
    assert path == tmp_path / "VIX1D.parquet"
    back = load_daily(tmp_path, ["VIX1D"])
    pd.testing.assert_frame_equal(back, daily)
    with pytest.raises(FileNotFoundError):
        load_daily(tmp_path, ["VIX9D"])


def test_fetch_daily_hits_expected_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=FIXTURE.read_text())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        frame = fetch_daily("VIX9D", client)
    assert seen == [CBOE_DAILY_URL.format(symbol="VIX9D")]
    assert (frame["symbol"] == "VIX9D").all()
    assert len(frame) == 6
