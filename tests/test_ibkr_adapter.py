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
    MAX_TICKERS_CODE,
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


# -- streaming market data -------------------------------------------------


def _spxw(strike: float = 6500.0, right: str = "C") -> Contract:
    c = Contract()
    c.symbol = "SPX"
    c.secType = "OPT"
    c.exchange = "SMART"
    c.currency = "USD"
    c.tradingClass = "SPXW"
    c.lastTradeDateOrContractMonth = "20260909"
    c.localSymbol = "SPXW  260909C06500000"
    c.conId = 907134620
    c.strike = strike
    c.right = right
    return c


def _subscribed(adapter: IBKRAdapter) -> int:
    """Subscribe without touching the socket; `reqMktData` needs a connection."""
    adapter.reqMktData = lambda *a, **k: None  # type: ignore[method-assign]
    adapter.cancelMktData = lambda *a, **k: None  # type: ignore[method-assign]
    return adapter.subscribe(_spxw())


def test_price_and_size_ticks_build_a_quote(adapter: IBKRAdapter) -> None:
    req_id = _subscribed(adapter)
    adapter.tickPrice(req_id, 1, 12.30, None)
    adapter.tickPrice(req_id, 2, 12.70, None)
    adapter.tickSize(req_id, 0, 40)
    adapter.tickSize(req_id, 3, 55)
    adapter.tickSize(req_id, 8, 28499)
    adapter.tickSize(req_id, 27, 1234)

    quote = adapter.quote(req_id)
    assert quote is not None
    assert (quote.bid, quote.ask) == (12.30, 12.70)
    assert (quote.bid_size, quote.ask_size) == (40.0, 55.0)
    assert quote.volume == 28499.0
    assert quote.open_interest == 1234.0
    assert quote.is_two_sided
    assert quote.delayed is False
    assert quote.con_id == 907134620
    assert quote.strike == 6500.0
    assert quote.ts is not None


def test_missing_quote_is_none_not_zero(adapter: IBKRAdapter) -> None:
    """TWS sends -1 for 'no quote'. A wing with no bid must not read as 0.00."""
    req_id = _subscribed(adapter)
    adapter.tickPrice(req_id, 1, -1.0, None)
    adapter.tickPrice(req_id, 2, 0.05, None)

    quote = adapter.quote(req_id)
    assert quote is not None
    assert quote.bid is None
    assert quote.ask == 0.05
    assert quote.is_two_sided is False


def test_delayed_ticks_are_mapped_and_flagged(adapter: IBKRAdapter) -> None:
    req_id = _subscribed(adapter)
    adapter.tickPrice(req_id, 66, 12.30, None)
    adapter.tickPrice(req_id, 67, 12.70, None)

    quote = adapter.quote(req_id)
    assert quote is not None
    assert (quote.bid, quote.ask) == (12.30, 12.70)
    assert quote.delayed is True


def test_market_data_type_three_flags_the_quote_as_delayed(adapter: IBKRAdapter) -> None:
    req_id = _subscribed(adapter)
    adapter.marketDataType(req_id, 3)
    assert adapter.quote(req_id).delayed is True


def test_model_option_computation_populates_greeks(adapter: IBKRAdapter) -> None:
    req_id = _subscribed(adapter)
    adapter.tickOptionComputation(
        req_id, 13, 0, 0.1432, -0.48, 12.5, 0.0, 0.0021, 0.34, -8.7, 6543.21
    )

    quote = adapter.quote(req_id)
    assert quote is not None
    assert quote.iv == pytest.approx(0.1432)
    assert quote.delta == pytest.approx(-0.48)
    assert quote.theta == pytest.approx(-8.7)
    assert quote.underlying == pytest.approx(6543.21)


def test_bid_and_ask_option_computations_are_ignored(adapter: IBKRAdapter) -> None:
    """Only the model tick is kept; it survives a one-sided market."""
    req_id = _subscribed(adapter)
    adapter.tickOptionComputation(
        req_id, 10, 0, 0.99, 0.9, 12.5, 0.0, 0.0, 0.0, 0.0, 6543.21
    )
    assert adapter.quote(req_id).iv is None


def test_uncomputable_greeks_become_none(adapter: IBKRAdapter) -> None:
    req_id = _subscribed(adapter)
    sentinel = 1.7976931348623157e308
    adapter.tickOptionComputation(
        req_id, 13, 0, -1.0, sentinel, sentinel, 0.0, sentinel, sentinel, sentinel, sentinel
    )

    quote = adapter.quote(req_id)
    assert quote is not None
    assert (quote.iv, quote.delta, quote.vega, quote.underlying) == (None, None, None, None)


def test_subscription_error_is_recorded_not_raised(adapter: IBKRAdapter) -> None:
    """A subscription has no blocking caller, so an error must land on the quote."""
    req_id = _subscribed(adapter)
    adapter.error(req_id, 0, 354, "Requested market data is not subscribed.", "")

    quote = adapter.quote(req_id)
    assert quote is not None
    assert quote.error == "Requested market data is not subscribed."


def test_max_tickers_error_reaches_the_caller(adapter: IBKRAdapter) -> None:
    """The line-budget refusal is identified by code, not by message text."""
    req_id = _subscribed(adapter)
    adapter.error(req_id, 0, MAX_TICKERS_CODE, "Max number of tickers has been reached.", "")

    quote = adapter.quote(req_id)
    assert quote.error_code == MAX_TICKERS_CODE
    assert "Max number of tickers" in quote.error


def test_unsubscribe_drops_the_state(adapter: IBKRAdapter) -> None:
    req_id = _subscribed(adapter)
    adapter.tickPrice(req_id, 1, 12.30, None)
    adapter.unsubscribe(req_id)

    assert adapter.quote(req_id) is None
    assert adapter.quotes() == {}
    # A tick arriving after the cancel is in flight, not an error.
    adapter.tickPrice(req_id, 1, 12.40, None)
    assert adapter.quotes() == {}


def test_unsubscribe_all_cancels_every_line(adapter: IBKRAdapter) -> None:
    cancelled: list[int] = []
    adapter.reqMktData = lambda *a, **k: None  # type: ignore[method-assign]
    adapter.cancelMktData = lambda rid: cancelled.append(rid)  # type: ignore[method-assign]
    ids = [adapter.subscribe(_spxw(strike)) for strike in (6490.0, 6495.0, 6500.0)]

    adapter.unsubscribe_all()
    assert cancelled == ids
    assert adapter.quotes() == {}
    adapter.unsubscribe_all()  # idempotent
    assert cancelled == ids


def test_ticks_for_an_unknown_request_are_ignored(adapter: IBKRAdapter) -> None:
    adapter.tickPrice(4242, 1, 12.30, None)
    adapter.tickSize(4242, 0, 10)
    adapter.marketDataType(4242, 3)
    assert adapter.quotes() == {}
