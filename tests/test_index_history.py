"""The IBKR backfill walk, against a fake adapter that serves bars keyed by `end`.

No network and no TWS. The fake records every `end` it was asked for so the
walk's direction, pacing gap and stop conditions can be asserted.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

pytest.importorskip("ibapi")

from spx0dte.features.bars import dates_on_disk, load_bars  # noqa: E402
from spx0dte.features.session import session_grid  # noqa: E402
from spx0dte.ingest.ibkr_adapter import Bar, IBKRError  # noqa: E402
from spx0dte.ingest.index_history import (  # noqa: E402
    END_FORMAT,
    PACING_BACKOFF,
    REQUEST_GAP,
    FetchPlan,
    SymbolUnavailable,
    bars_to_frame,
    fetch_symbol,
    probe,
    request_bars,
)

TODAY = date(2026, 9, 11)  # a Friday
SESSIONS = [date(2026, 9, 4), date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10), TODAY]


def session_bars(day: date, level: float = 100.0) -> list[Bar]:
    return [Bar(ts.to_pydatetime(), level, level + 1, level - 1, level, 0.0, level, 1)
            for ts in session_grid(day)]


class FakeAdapter:
    """Serves whole sessions whose bars start before `end`, newest `chunk_sessions` of them."""

    def __init__(self, sessions: list[date], chunk_sessions: int = 2,
                 errors: list[IBKRError] | None = None) -> None:
        self.sessions = sessions
        self.chunk_sessions = chunk_sessions
        self.errors = list(errors or [])
        self.ends: list[str] = []

    def historical_bars(self, contract, *, end="", duration, bar_size, what_to_show, use_rth):
        self.ends.append(end)
        if self.errors:
            raise self.errors.pop(0)
        if not end:
            cutoff = datetime.now(UTC)
        else:
            cutoff = datetime.strptime(end, END_FORMAT).replace(tzinfo=UTC)
        eligible = [d for d in self.sessions if session_grid(d)[0] < cutoff]
        out: list[Bar] = []
        for day in eligible[-self.chunk_sessions:]:
            out.extend(b for b in session_bars(day) if b.ts < cutoff)
        return out


def test_bars_to_frame_round_trips_and_empty_is_empty() -> None:
    frame = bars_to_frame(session_bars(TODAY), "SPX")
    assert len(frame) == 390 and (frame["symbol"] == "SPX").all()
    assert str(frame["ts"].dtype) == "datetime64[us, UTC]"
    assert bars_to_frame([], "SPX").empty


def test_walk_goes_backwards_writes_one_part_per_date_and_skips_today(tmp_path) -> None:
    api = FakeAdapter(SESSIONS)
    sleeps: list[float] = []
    written = fetch_symbol(api, "SPX", FetchPlan(days=4), tmp_path,  # type: ignore[arg-type]
                           sleep=sleeps.append, today=TODAY)
    assert written == 4
    assert dates_on_disk(tmp_path, "SPX") == set(SESSIONS[:-1])
    assert api.ends[0] == ""
    parsed = [datetime.strptime(e, END_FORMAT) for e in api.ends[1:]]
    assert parsed == sorted(parsed, reverse=True)
    assert all(s == REQUEST_GAP for s in sleeps)
    assert len(load_bars(tmp_path)) == 4 * 390


def test_second_run_skips_dates_on_disk_unless_forced(tmp_path) -> None:
    api = FakeAdapter(SESSIONS)
    fetch_symbol(api, "SPX", FetchPlan(days=4), tmp_path, sleep=lambda _: None, today=TODAY)  # type: ignore[arg-type]
    before = sorted(tmp_path.rglob("part-*.parquet"))

    again = FakeAdapter(SESSIONS)
    written = fetch_symbol(again, "SPX", FetchPlan(days=4), tmp_path,  # type: ignore[arg-type]
                           sleep=lambda _: None, today=TODAY)
    assert written == 0
    assert sorted(tmp_path.rglob("part-*.parquet")) == before

    forced = FakeAdapter(SESSIONS)
    written = fetch_symbol(forced, "SPX", FetchPlan(days=4, force=True), tmp_path,  # type: ignore[arg-type]
                           sleep=lambda _: None, today=TODAY)
    assert written == 4
    assert len(load_bars(tmp_path)) == 4 * 390  # later parts win, no duplicates


def test_include_today_writes_the_partial_session(tmp_path) -> None:
    api = FakeAdapter(SESSIONS)
    fetch_symbol(api, "SPX", FetchPlan(days=2, include_today=True), tmp_path,  # type: ignore[arg-type]
                 sleep=lambda _: None, today=TODAY)
    assert TODAY in dates_on_disk(tmp_path, "SPX")


def test_no_data_twice_stops_the_walk(tmp_path) -> None:
    no_data = IBKRError(162, "HMDS query returned no data: SPX@CBOE Trades")
    api = FakeAdapter([], errors=[no_data, no_data, no_data])
    written = fetch_symbol(api, "SPX", FetchPlan(days=10), tmp_path,  # type: ignore[arg-type]
                           sleep=lambda _: None, today=TODAY)
    assert written == 0
    assert len(api.ends) == 2


def test_pacing_backs_off_once_and_retries() -> None:
    pacing = IBKRError(162, "Historical Market Data Service error message:pacing violation")
    api = FakeAdapter(SESSIONS, errors=[pacing])
    sleeps: list[float] = []
    bars = request_bars(api, "SPX", end="", duration="2 D", sleep=sleeps.append)  # type: ignore[arg-type]
    assert sleeps == [PACING_BACKOFF]
    assert len(api.ends) == 2 and bars


def test_unknown_contract_is_symbol_unavailable() -> None:
    api = FakeAdapter(SESSIONS, errors=[IBKRError(200, "No security definition has been found")])
    with pytest.raises(SymbolUnavailable, match="200"):
        request_bars(api, "VIX1D", end="", duration="2 D", sleep=lambda _: None)  # type: ignore[arg-type]


def test_probe_reports_per_symbol_and_continues_past_failures() -> None:
    api = FakeAdapter(SESSIONS, errors=[IBKRError(200, "No security definition has been found")])
    report = probe(api, ["VIX1D", "SPX"], sleep=lambda _: None)  # type: ignore[arg-type]
    assert report["VIX1D"].startswith("UNAVAILABLE")
    assert report["SPX"].startswith("780 bars")


def test_walk_stops_once_enough_sessions_are_on_disk(tmp_path) -> None:
    api = FakeAdapter(SESSIONS)
    fetch_symbol(api, "SPX", FetchPlan(days=2), tmp_path, sleep=lambda _: None, today=TODAY)  # type: ignore[arg-type]
    # Two sessions wanted; today is skipped, so the walk needs a second chunk and
    # keeps whatever that chunk returned rather than throwing bars away.
    assert {date(2026, 9, 9), date(2026, 9, 10)} <= dates_on_disk(tmp_path, "SPX")
    assert date(2026, 9, 4) not in dates_on_disk(tmp_path, "SPX")
    assert len(api.ends) == 2


def test_fake_adapter_serves_older_sessions_for_older_end() -> None:
    api = FakeAdapter(SESSIONS)
    end = (session_grid(date(2026, 9, 9))[0] - timedelta(seconds=1)).strftime(END_FORMAT)
    bars = api.historical_bars(None, end=end, duration="2 D", bar_size="1 min",
                               what_to_show="TRADES", use_rth=True)
    days = {b.ts.date() for b in bars}
    assert days == {date(2026, 9, 4), date(2026, 9, 8)}
