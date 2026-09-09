"""Unit tests for the chain recorder.

No network and no TWS: the adapter is replaced by a stub that serves quotes
from a dict, which is all the recorder ever reads from it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import time as clock_time
from pathlib import Path

import pytest

pytest.importorskip("ibapi")

import pyarrow.parquet as pq  # noqa: E402

from spx0dte.ingest.ibkr_adapter import (  # noqa: E402
    MAX_TICKERS_CODE,
    Bar,
    ContractDetail,
    IBKRTimeoutError,
    Quote,
)
from spx0dte.ingest.recorder import (  # noqa: E402
    ParquetSink,
    Recorder,
    SpotPoll,
    atm_first,
    contract_from,
    rows_from_quotes,
    session_end,
    today_expiry,
)

NOW = datetime(2026, 9, 9, 18, 30, tzinfo=UTC)  # 14:30 ET


def quote(strike: float = 6500.0, **overrides) -> Quote:
    base = {
        "con_id": int(strike),
        "error_code": None,
        "local_symbol": f"SPXW  260909C0{strike:.0f}000",
        "right": "C",
        "strike": strike,
        "expiry": "20260909",
        "ts": NOW,
        "bid": 12.30,
        "ask": 12.70,
        "iv": 0.1593,
    }
    return Quote(**{**base, **overrides})


def detail(strike: float, right: str = "C") -> ContractDetail:
    return ContractDetail(
        con_id=int(strike) * 10 + (1 if right == "C" else 2),
        local_symbol=f"SPXW  260909{right}0{strike:.0f}000",
        symbol="SPX",
        sec_type="OPT",
        exchange="SMART",
        currency="USD",
        last_trade_date="20260909",
        strike=strike,
        right=right,
        multiplier="100",
        trading_hours="",
    )


class FakeAdapter:
    """The slice of IBKRAdapter the recorder uses."""

    def __init__(self, quotes: dict[int, Quote] | None = None) -> None:
        self._quotes = quotes or {}
        self.is_connected = True
        self.subscribed: list[int] = []
        self.cancelled: list[int] = []
        self.bars: list[Bar] = []
        self.bar_calls = 0
        self.raises: Exception | None = None

    def subscribe(self, contract) -> int:
        """Mirrors the adapter: the quote's identity comes off the contract."""
        req_id = 9000 + len(self.subscribed)
        self.subscribed.append(contract.conId)
        self._quotes[req_id] = quote(
            con_id=contract.conId,
            strike=contract.strike,
            right=contract.right,
            local_symbol=contract.localSymbol,
            expiry=contract.lastTradeDateOrContractMonth,
        )
        return req_id

    def unsubscribe(self, req_id: int) -> None:
        self.cancelled.append(req_id)
        self._quotes.pop(req_id, None)

    def quotes(self) -> dict[int, Quote]:
        return dict(self._quotes)

    def historical_bars(self, *args, **kwargs) -> list[Bar]:
        self.bar_calls += 1
        if self.raises is not None:
            raise self.raises
        return self.bars


# -- exchange-time helpers -------------------------------------------------


def test_expiry_follows_exchange_date_not_utc() -> None:
    """01:00 UTC is still the previous trading day in New York."""
    assert today_expiry(datetime(2026, 9, 10, 1, 0, tzinfo=UTC)) == "20260909"


def test_session_end_is_the_1615_et_close() -> None:
    end = session_end(NOW)
    assert end == datetime(2026, 9, 9, 20, 15, tzinfo=UTC)  # 16:15 EDT


def test_session_end_accepts_an_earlier_stop() -> None:
    end = session_end(NOW, end=clock_time(12, 0))
    assert end == datetime(2026, 9, 9, 16, 0, tzinfo=UTC)


# -- ordering --------------------------------------------------------------


def test_chain_is_ordered_outwards_from_the_money() -> None:
    details = [detail(k, r) for k in (6400.0, 6600.0, 6500.0, 6510.0) for r in ("C", "P")]
    ordered = atm_first(details, centre=6505.0)
    assert [d.strike for d in ordered][:4] == [6500.0, 6500.0, 6510.0, 6510.0]
    assert [d.strike for d in ordered][-2:] == [6400.0, 6400.0]


# -- rows ------------------------------------------------------------------


def test_missing_fields_stay_null_in_the_row() -> None:
    rows = rows_from_quotes([quote(bid=None, ask=0.05)], spot=6543.2, spot_ts=NOW, ts=NOW)
    assert rows[0]["bid"] is None
    assert rows[0]["ask"] == 0.05
    assert rows[0]["spot"] == 6543.2


def test_row_carries_the_sample_time_and_the_quote_time() -> None:
    stale = datetime(2026, 9, 9, 18, 0, tzinfo=UTC)
    rows = rows_from_quotes([quote(ts=stale)], spot=None, spot_ts=None, ts=NOW)
    assert rows[0]["ts"] == NOW
    assert rows[0]["quote_ts"] == stale
    assert rows[0]["spot"] is None


# -- sink ------------------------------------------------------------------


def test_flush_writes_a_partitioned_part_file(tmp_path: Path) -> None:
    sink = ParquetSink(tmp_path)
    sink.add(rows_from_quotes([quote(), quote(6505.0)], 6543.2, NOW, NOW))
    path = sink.flush(NOW)

    assert path == tmp_path / "date=2026-09-09" / "part-143000.parquet"
    table = pq.read_table(path)
    assert table.num_rows == 2
    assert sink.buffer == []


def test_nulls_survive_the_round_trip(tmp_path: Path) -> None:
    """A wing with no bid must read back as null, not as 0.0."""
    sink = ParquetSink(tmp_path)
    sink.add(rows_from_quotes([quote(bid=None, iv=None)], None, None, NOW))
    table = pq.read_table(sink.flush(NOW))

    row = table.to_pylist()[0]
    assert row["bid"] is None
    assert row["iv"] is None
    assert row["spot"] is None
    assert row["ask"] == 12.70


def test_flushing_an_empty_buffer_writes_nothing(tmp_path: Path) -> None:
    assert ParquetSink(tmp_path).flush(NOW) is None
    assert list(tmp_path.iterdir()) == []


# -- spot ------------------------------------------------------------------


def test_spot_is_cached_between_polls() -> None:
    api = FakeAdapter()
    api.bars = [Bar(NOW, 6540.0, 6545.0, 6539.0, 6543.2, 0.0, 0.0, 0)]
    poll = SpotPoll(api)  # type: ignore[arg-type]

    assert poll.value() == (6543.2, NOW)
    assert poll.value() == (6543.2, NOW)
    assert api.bar_calls == 1


def test_failed_poll_keeps_the_previous_value_and_its_timestamp() -> None:
    api = FakeAdapter()
    api.bars = [Bar(NOW, 6540.0, 6545.0, 6539.0, 6543.2, 0.0, 0.0, 0)]
    poll = SpotPoll(api, max_age=0.0)  # type: ignore[arg-type]
    poll.value()

    api.raises = IBKRTimeoutError("no answer")
    assert poll.value() == (6543.2, NOW)


def test_spot_is_none_until_a_bar_arrives() -> None:
    poll = SpotPoll(FakeAdapter(), max_age=0.0)  # type: ignore[arg-type]
    assert poll.value() == (None, None)


# -- recorder --------------------------------------------------------------


def test_sample_writes_one_row_per_subscription(tmp_path: Path) -> None:
    api = FakeAdapter({1: quote(6500.0), 2: quote(6505.0)})
    recorder = Recorder(api, ParquetSink(tmp_path), flush_every=10)  # type: ignore[arg-type]

    assert recorder.sample(NOW) == 2
    assert list(tmp_path.iterdir()) == []  # buffered, not yet written


def test_buffer_is_flushed_every_n_samples(tmp_path: Path) -> None:
    api = FakeAdapter({1: quote()})
    sink = ParquetSink(tmp_path)
    recorder = Recorder(api, sink, flush_every=3)  # type: ignore[arg-type]

    for _ in range(3):
        recorder.sample(NOW)
    assert len(list((tmp_path / "date=2026-09-09").iterdir())) == 1
    assert sink.buffer == []


def test_recorded_rows_carry_the_contract_identity(tmp_path: Path) -> None:
    """Subscribing by conId alone records a chain of blank strikes."""
    api = FakeAdapter()
    recorder = Recorder(api, ParquetSink(tmp_path))  # type: ignore[arg-type]
    details = [detail(6500.0, "C"), detail(6505.0, "P")]

    recorder.subscribe_chain(details, settle=0.0)
    rows = rows_from_quotes(api.quotes().values(), 6543.2, NOW, NOW)

    assert sorted(r["strike"] for r in rows) == [6500.0, 6505.0]
    assert sorted(r["right"] for r in rows) == ["C", "P"]
    assert all(r["expiry"] == "20260909" and r["local_symbol"] for r in rows)


def test_contract_from_keeps_every_resolved_field() -> None:
    contract = contract_from(detail(6500.0, "P"))
    assert (contract.strike, contract.right, contract.secType) == (6500.0, "P", "OPT")
    assert contract.conId == detail(6500.0, "P").con_id
    assert contract.exchange == "SMART"


def test_entitlement_notice_is_not_recorded_as_a_row_error() -> None:
    """10090 arrives on every subscription; a real error must stay visible."""
    rows = rows_from_quotes(
        [quote(error_code=10090), quote(6505.0, error_code=354)], None, None, NOW
    )
    assert rows[0]["error_code"] is None
    assert rows[1]["error_code"] == 354


def test_refused_lines_are_dropped_so_the_money_is_kept(tmp_path: Path) -> None:
    """Error 101 costs the far wing, because subscriptions open ATM-first."""
    api = FakeAdapter()
    recorder = Recorder(api, ParquetSink(tmp_path))  # type: ignore[arg-type]
    details = atm_first([detail(k) for k in (6500.0, 6600.0, 6400.0)], centre=6500.0)

    def subscribe(contract):
        req_id = 9000 + len(api.subscribed)
        api.subscribed.append(contract.conId)
        refused = contract.conId != details[0].con_id
        api._quotes[req_id] = quote(
            strike=float(contract.conId),
            error="Max number of tickers has been reached." if refused else None,
            error_code=MAX_TICKERS_CODE if refused else None,
        )
        return req_id

    api.subscribe = subscribe  # type: ignore[method-assign]
    opened = recorder.subscribe_chain(details, settle=0.0)

    assert opened == 1
    assert len(api.cancelled) == 2
    assert list(api.quotes().values())[0].strike == float(details[0].con_id)


def test_run_stops_when_the_connection_drops(tmp_path: Path) -> None:
    """Sampling a dead connection would write stale quotes that look valid."""
    api = FakeAdapter({1: quote()})
    api.is_connected = False
    sink = ParquetSink(tmp_path)
    recorder = Recorder(api, sink, interval=0.01)  # type: ignore[arg-type]

    recorder.run(until=datetime(2099, 1, 1, tzinfo=UTC))

    assert recorder.samples == 0
    assert sink.buffer == []
