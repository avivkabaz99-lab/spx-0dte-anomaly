"""Unit tests for the read-only IBKR adapter.

No network. Each test replaces the outbound `reqX` call with a stub that fires
the EWrapper callbacks from another thread, which is exactly what TWS does, so
the blocking public methods are exercised for real.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

pytest.importorskip("ibapi")

from ibapi.contract import Contract  # noqa: E402

from spx0dte.config import IBKRConfig  # noqa: E402
from spx0dte.ingest.ibkr_adapter import (  # noqa: E402
    IBKRAdapter,
    IBKRError,
    IBKRTimeoutError,
    _split_error_args,
    parse_bar_timestamp,
)


@dataclass
class FakeBar:
    """Mirrors the attributes of `ibapi.common.BarData` that the adapter reads."""

    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    wap: float
    barCount: int  # noqa: N815 - matches the ibapi attribute name


@pytest.fixture()
def adapter() -> IBKRAdapter:
    config = IBKRConfig(host="127.0.0.1", port=7497, client_id=99)
    return IBKRAdapter(config, timeout=2.0)


def _respond(adapter: IBKRAdapter, callbacks) -> None:
    """Fire callbacks on a separate thread, as the TWS reader thread would."""
    threading.Thread(target=lambda: [cb() for cb in callbacks], daemon=True).start()


# -- timestamp parsing -----------------------------------------------------


def test_parses_epoch_timestamp() -> None:
    assert parse_bar_timestamp("1757408400") == datetime.fromtimestamp(1757408400, tz=UTC)


def test_parses_daily_date_string() -> None:
    assert parse_bar_timestamp("20260909") == datetime(2026, 9, 9, tzinfo=UTC)


def test_parses_date_with_trailing_time_zone() -> None:
    assert parse_bar_timestamp("20260909 09:30:00 US/Eastern") == datetime(2026, 9, 9, tzinfo=UTC)


def test_rejects_unparseable_timestamp() -> None:
    with pytest.raises(ValueError, match="unrecognised bar timestamp"):
        parse_bar_timestamp("not-a-date")


# -- error argument layouts ------------------------------------------------


def test_error_args_parsed_for_modern_signature() -> None:
    """TWS API >= 10.30 sends (errorTime, errorCode, errorString)."""
    assert _split_error_args((0, 200, "No security definition", "")) == (
        200,
        "No security definition",
    )


def test_error_args_parsed_for_legacy_signature() -> None:
    """TWS API <= 10.29 sends (errorCode, errorString)."""
    assert _split_error_args((200, "No security definition")) == (
        200,
        "No security definition",
    )


def test_error_args_without_a_message() -> None:
    assert _split_error_args((0, 504)) == (504, "")


# -- historical bars -------------------------------------------------------


def test_historical_bars_returns_parsed_rows(adapter: IBKRAdapter) -> None:
    bar = FakeBar("1757408400", 1.0, 2.0, 0.5, 1.5, 10, 1.25, 4)

    def fake_request(req_id, *args, **kwargs):
        _respond(adapter, [
            lambda: adapter.historicalData(req_id, bar),
            lambda: adapter.historicalDataEnd(req_id, "", ""),
        ])

    adapter.reqHistoricalData = fake_request
    rows = adapter.historical_bars(Contract())

    assert len(rows) == 1
    assert rows[0].close == 1.5
    assert rows[0].count == 4
    assert rows[0].ts == datetime.fromtimestamp(1757408400, tz=UTC)


def test_unset_numeric_sentinel_becomes_zero(adapter: IBKRAdapter) -> None:
    """IBKR sends a huge sentinel rather than null for missing values."""
    bar = FakeBar("20260909", 1.0, 2.0, 0.5, 1.5, 1.7e308, 0.0, 0)

    def fake_request(req_id, *args, **kwargs):
        _respond(adapter, [
            lambda: adapter.historicalData(req_id, bar),
            lambda: adapter.historicalDataEnd(req_id, "", ""),
        ])

    adapter.reqHistoricalData = fake_request
    assert adapter.historical_bars(Contract())[0].volume == 0.0


def test_error_from_tws_is_raised(adapter: IBKRAdapter) -> None:
    def fake_request(req_id, *args, **kwargs):
        _respond(adapter, [lambda: adapter.error(req_id, 0, 200, "No security definition")])

    adapter.reqHistoricalData = fake_request
    with pytest.raises(IBKRError) as excinfo:
        adapter.historical_bars(Contract())

    assert excinfo.value.code == 200
    assert "No security definition" in excinfo.value.message


def test_informational_code_does_not_abort_the_request(adapter: IBKRAdapter) -> None:
    """Codes 2100-2200 are data-farm status messages, not failures."""
    bar = FakeBar("20260909", 1.0, 1.0, 1.0, 1.0, 0, 0, 0)

    def fake_request(req_id, *args, **kwargs):
        _respond(adapter, [
            lambda: adapter.error(req_id, 0, 2106, "HMDS data farm connection is OK"),
            lambda: adapter.historicalData(req_id, bar),
            lambda: adapter.historicalDataEnd(req_id, "", ""),
        ])

    adapter.reqHistoricalData = fake_request
    assert len(adapter.historical_bars(Contract())) == 1


def test_silent_request_times_out(adapter: IBKRAdapter) -> None:
    adapter.reqHistoricalData = lambda *args, **kwargs: None
    with pytest.raises(IBKRTimeoutError, match="did not complete"):
        adapter.historical_bars(Contract())


def test_request_state_is_cleaned_up_after_completion(adapter: IBKRAdapter) -> None:
    """A leaked request dict would grow without bound over a recording session."""
    adapter.reqHistoricalData = lambda *args, **kwargs: None
    with pytest.raises(IBKRTimeoutError):
        adapter.historical_bars(Contract())

    assert adapter._requests == {}


# -- contract details ------------------------------------------------------


def test_contract_details_are_flattened(adapter: IBKRAdapter) -> None:
    class FakeDetails:
        def __init__(self) -> None:
            self.contract = Contract()
            self.contract.conId = 123
            self.contract.localSymbol = "SPXW  260909C06500000"
            self.contract.symbol = "SPX"
            self.contract.secType = "OPT"
            self.contract.exchange = "CBOE"
            self.contract.currency = "USD"
            self.contract.lastTradeDateOrContractMonth = "20260909"
            self.contract.strike = 6500.0
            self.contract.right = "C"
            self.contract.multiplier = "100"
            self.tradingHours = "20260909:0930-20260909:1615"

    def fake_request(req_id, *args, **kwargs):
        _respond(adapter, [
            lambda: adapter.contractDetails(req_id, FakeDetails()),
            lambda: adapter.contractDetailsEnd(req_id),
        ])

    adapter.reqContractDetails = fake_request
    rows = adapter.contract_details(Contract())

    assert len(rows) == 1
    assert rows[0].con_id == 123
    assert rows[0].strike == 6500.0
    assert rows[0].last_trade_date == "20260909"


# -- read-only guard -------------------------------------------------------


def test_place_order_is_blocked(adapter: IBKRAdapter) -> None:
    """The adapter may point at a live account; ordering must be impossible."""
    with pytest.raises(RuntimeError, match="read-only"):
        adapter.placeOrder(1, Contract(), object())


def test_request_ids_are_unique(adapter: IBKRAdapter) -> None:
    adapter.reqHistoricalData = lambda *args, **kwargs: None
    seen = []
    for _ in range(3):
        request = adapter._start(lambda rid: seen.append(rid))
        request.done.set()
        adapter._finish(request)

    assert len(set(seen)) == 3
