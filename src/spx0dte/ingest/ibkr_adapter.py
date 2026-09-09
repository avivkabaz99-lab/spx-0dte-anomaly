"""Read-only adapter over IBKR's callback-based `ibapi` client.

`ibapi` delivers every result through `EWrapper` callbacks on a reader thread.
This adapter turns that into blocking calls that return plain dataclasses, so
nothing downstream has to know callbacks exist.

The adapter is deliberately read-only. It wraps contract lookup, historical
bars and streaming market data only, and `placeOrder` is overridden to raise.
See the hard rules in CLAUDE.md: the IBKR connection never places an order.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ibapi.client import EClient
from ibapi.contract import Contract, ContractDetails
from ibapi.wrapper import EWrapper

from spx0dte.config import IBKRConfig

logger = logging.getLogger(__name__)

# TWS reports connection status and data-farm health through the same `error`
# channel as real failures. Codes in this band are informational.
_INFO_CODE_MIN = 2100
_INFO_CODE_MAX = 2200

# Request ids for market-data requests are ours to allocate and are unrelated to
# order ids. Starting high keeps them clearly distinct in TWS logs.
_FIRST_REQUEST_ID = 9000

# Tick ids from `ibapi.ticktype.TickTypeEnum`. Delayed data arrives under its
# own set of ids, so both are mapped onto the same field and the ticker is
# flagged, rather than silently recording delayed prices as live ones.
_PRICE_TICKS: dict[int, str] = {
    1: "bid", 2: "ask", 4: "last",
    66: "bid", 67: "ask", 68: "last",
}
_SIZE_TICKS: dict[int, str] = {
    0: "bid_size", 3: "ask_size", 5: "last_size", 8: "volume",
    27: "open_interest", 28: "open_interest",
    69: "bid_size", 70: "ask_size", 71: "last_size", 74: "volume",
}
_DELAYED_TICKS = frozenset({66, 67, 68, 69, 70, 71, 74, 80, 81, 82, 83})

# Option computations arrive once per side. The model tick is the one that
# carries a full set of greeks even when the contract is one-sided.
_MODEL_OPTION_TICKS = frozenset({13, 83})

# TWS refuses a subscription past the account's market-data line budget with
# this code. Measuring that number is the point of scripts/spike_marketdata.py.
MAX_TICKERS_CODE = 101

# Partial-entitlement notices. TWS sends one per subscription and then streams
# the ticks it *is* entitled to, so these describe the feed rather than a
# failure: 10090 is "part of this data is not subscribed", 10167 is the
# delayed-data substitution. They are recorded on the quote, but at one
# subscription per strike they would otherwise flood the log at WARNING.
MARKET_DATA_NOTICE_CODES = frozenset({10090, 10091, 10167})


class IBKRError(RuntimeError):
    """An error TWS returned for a specific request."""

    def __init__(self, code: int, message: str, req_id: int | None = None) -> None:
        self.code = code
        self.message = message
        self.req_id = req_id
        super().__init__(f"TWS error {code} on request {req_id}: {message}")


class IBKRTimeoutError(RuntimeError):
    """A request did not complete within the configured timeout."""


@dataclass(frozen=True)
class Bar:
    """One historical bar, with IBKR's Decimal fields narrowed to float."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    wap: float
    count: int


@dataclass(frozen=True)
class ContractDetail:
    """The subset of `ContractDetails` this project uses."""

    con_id: int
    local_symbol: str
    symbol: str
    sec_type: str
    exchange: str
    currency: str
    last_trade_date: str
    strike: float
    right: str
    multiplier: str
    trading_hours: str


@dataclass(frozen=True)
class Quote:
    """One contract's market data as of `ts`.

    Every numeric field is None until TWS sends it. Missing is not zero: an
    option with no bid and an option bid at 0.00 are different states, and
    recording one as the other would corrupt every spread computed downstream.
    """

    con_id: int
    local_symbol: str
    right: str
    strike: float
    expiry: str
    ts: datetime | None = None
    bid: float | None = None
    bid_size: float | None = None
    ask: float | None = None
    ask_size: float | None = None
    last: float | None = None
    last_size: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None
    underlying: float | None = None
    delayed: bool = False
    error: str | None = None
    error_code: int | None = None

    @property
    def is_two_sided(self) -> bool:
        """True when both sides are quoted, i.e. the row is usable for a spread."""
        return self.bid is not None and self.ask is not None


class _Request:
    """Mutable state for one in-flight request, resolved by callbacks."""

    def __init__(self, req_id: int) -> None:
        self.req_id = req_id
        self.done = threading.Event()
        self.rows: list[Any] = []
        self.error: IBKRError | None = None


class _Ticker:
    """Mutable state for one streaming subscription, updated by callbacks.

    Unlike `_Request` a subscription has no end: it lives until cancelled, and
    an error on it is recorded rather than raised, because there is no blocking
    caller waiting to receive it.
    """

    def __init__(self, req_id: int, contract: Contract) -> None:
        self.req_id = req_id
        self.contract = contract
        self.values: dict[str, float | None] = {}
        self.ts: datetime | None = None
        self.delayed = False
        self.error: IBKRError | None = None

    def set(self, field: str, value: float | None, *, delayed: bool = False) -> None:
        self.values[field] = value
        self.ts = datetime.now(UTC)
        if delayed:
            self.delayed = True


def _optional_float(value: Any, *, allow_negative: bool = True) -> float | None:
    """Narrow one IBKR numeric into a float, or None when it means 'no value'.

    IBKR signals 'unset' two different ways: a huge sentinel near DBL_MAX, and
    -1 on price, size and implied-vol ticks. Greeks are legitimately negative,
    so the -1 rule is applied only where the caller says it applies.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if abs(out) > 1e17:
        return None
    if not allow_negative and out < 0:
        return None
    return out


def _to_float(value: Any) -> float:
    """IBKR returns Decimal for size fields and a sentinel for 'unset'."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    # ibapi uses a huge sentinel rather than None for missing numeric values.
    return 0.0 if abs(out) > 1e17 else out


def parse_bar_timestamp(raw: str) -> datetime:
    """Parse a `BarData.date` into an aware UTC datetime.

    Requests use `formatDate=2`, so the value is normally epoch seconds. Daily
    bars and some server versions still return a date string, so both are
    handled.
    """
    text = str(raw).strip()
    if text.isdigit() and len(text) >= 9:
        return datetime.fromtimestamp(int(text), tz=UTC)
    head = text.split()[0] if text else ""
    if len(head) == 8 and head.isdigit():
        return datetime.strptime(head, "%Y%m%d").replace(tzinfo=UTC)
    raise ValueError(f"unrecognised bar timestamp: {raw!r}")


class IBKRAdapter(EWrapper, EClient):
    """Blocking, read-only access to TWS or IB Gateway.

    Args:
        config: Host, port and client id. Port 7497 is TWS paper; 7496 and 4001
            are not assumed to be paper.
        timeout: Seconds to wait for any single request, and for the initial
            connection handshake.
    """

    def __init__(self, config: IBKRConfig, timeout: float = 30.0) -> None:
        EWrapper.__init__(self)
        EClient.__init__(self, wrapper=self)
        self._config = config
        self._timeout = timeout
        self._lock = threading.Lock()
        self._requests: dict[int, _Request] = {}
        self._subscriptions: dict[int, _Ticker] = {}
        self._next_request_id = _FIRST_REQUEST_ID
        self._reader: threading.Thread | None = None
        self._ready = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Connect and start the reader thread, blocking until TWS is ready."""
        self.connect(self._config.host, self._config.port, self._config.client_id)
        self._reader = threading.Thread(target=self.run, name="ibapi-reader", daemon=True)
        self._reader.start()
        if not self._ready.wait(self._timeout):
            self.close()
            raise IBKRTimeoutError(
                f"no handshake from {self._config.host}:{self._config.port} "
                f"within {self._timeout:g}s; is TWS or IB Gateway running and the "
                "API enabled?"
            )

    def close(self) -> None:
        """Cancel every subscription, disconnect, and join the reader thread."""
        try:
            self.unsubscribe_all()
            self.disconnect()
        finally:
            if self._reader is not None:
                self._reader.join(timeout=5.0)
                self._reader = None

    @property
    def is_connected(self) -> bool:
        """False once TWS drops the socket, including its own daily restart."""
        return bool(self.isConnected())

    def __enter__(self) -> IBKRAdapter:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- read-only guard ---------------------------------------------------

    def placeOrder(self, *args: object, **kwargs: object) -> None:  # noqa: N802
        """Refuse to place orders.

        The adapter may be pointed at a live account, so this is a hard block
        rather than a convention.
        """
        raise RuntimeError(
            "IBKRAdapter is read-only and cannot place orders; "
            "implementing execution requires an explicit decision (see CLAUDE.md)"
        )

    # -- request plumbing --------------------------------------------------

    def _start(self, send: Callable[[int], None]) -> _Request:
        with self._lock:
            req_id = self._next_request_id
            self._next_request_id += 1
            request = _Request(req_id)
            self._requests[req_id] = request
        send(req_id)
        return request

    def _finish(self, request: _Request) -> list[Any]:
        try:
            if not request.done.wait(self._timeout):
                raise IBKRTimeoutError(
                    f"request {request.req_id} did not complete within {self._timeout:g}s"
                )
            if request.error is not None:
                raise request.error
            return request.rows
        finally:
            with self._lock:
                self._requests.pop(request.req_id, None)

    def _lookup(self, req_id: int) -> _Request | None:
        with self._lock:
            return self._requests.get(req_id)

    # -- public reads ------------------------------------------------------

    def contract_details(self, contract: Contract) -> list[ContractDetail]:
        """Resolve a contract, returning every match TWS knows about."""
        request = self._start(lambda rid: self.reqContractDetails(rid, contract))
        return [_as_contract_detail(row) for row in self._finish(request)]

    def historical_bars(
        self,
        contract: Contract,
        *,
        end: str = "",
        duration: str = "1 D",
        bar_size: str = "1 min",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
    ) -> list[Bar]:
        """Fetch historical bars.

        Args:
            contract: A fully qualified contract.
            end: Request end time, `YYYYMMDD-HH:MM:SS` in UTC, or "" for now.
            duration: IBKR duration string, e.g. "1 D", "2 W".
            bar_size: IBKR bar size, e.g. "1 min", "1 hour", "1 day".
            what_to_show: TRADES, MIDPOINT, BID, ASK, OPTION_IMPLIED_VOLATILITY.
            use_rth: Restrict to regular trading hours.
        """
        request = self._start(
            lambda rid: self.reqHistoricalData(
                rid, contract, end, duration, bar_size, what_to_show,
                int(use_rth), 2, False, [],
            )
        )
        return [_as_bar(row) for row in self._finish(request)]

    # -- streaming market data ---------------------------------------------

    def set_market_data_type(self, kind: int = 1) -> None:
        """Choose the data TWS falls back to: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen.

        This is a preference, not a guarantee. TWS answers with the type it
        actually served through `marketDataType`, which lands on the quote as
        `delayed`.
        """
        self.reqMarketDataType(kind)

    def subscribe(self, contract: Contract, *, generic_ticks: str = "100,101") -> int:
        """Open a streaming subscription and return its request id.

        The subscription holds one market-data line until `unsubscribe`. Ticks
        update state in the background; read it with `quotes()`.

        Args:
            contract: A fully qualified contract.
            generic_ticks: IBKR generic tick list. 100 is option volume and 101
                option open interest; greeks arrive without being asked for.
        """
        with self._lock:
            req_id = self._next_request_id
            self._next_request_id += 1
            self._subscriptions[req_id] = _Ticker(req_id, contract)
        self.reqMktData(req_id, contract, generic_ticks, False, False, [])
        return req_id

    def unsubscribe(self, req_id: int) -> None:
        """Cancel one subscription and release its line."""
        with self._lock:
            ticker = self._subscriptions.pop(req_id, None)
        if ticker is not None:
            self.cancelMktData(req_id)

    def unsubscribe_all(self) -> None:
        """Cancel every subscription. Safe to call twice."""
        with self._lock:
            req_ids = list(self._subscriptions)
            self._subscriptions.clear()
        for req_id in req_ids:
            self.cancelMktData(req_id)

    def quotes(self) -> dict[int, Quote]:
        """Point-in-time copy of every subscription, keyed by request id."""
        with self._lock:
            return {rid: _as_quote(t) for rid, t in self._subscriptions.items()}

    def quote(self, req_id: int) -> Quote | None:
        """One subscription's current state, or None if it is not subscribed."""
        with self._lock:
            ticker = self._subscriptions.get(req_id)
            return _as_quote(ticker) if ticker is not None else None

    def _tick(self, req_id: int) -> _Ticker | None:
        with self._lock:
            return self._subscriptions.get(req_id)

    # -- EWrapper callbacks ------------------------------------------------

    def nextValidId(self, orderId: int) -> None:  # noqa: N802, N803
        """Handshake complete. The id itself is unused: we place no orders."""
        self._ready.set()

    def contractDetails(self, reqId: int, contractDetails: ContractDetails) -> None:  # noqa: N802, N803
        request = self._lookup(reqId)
        if request is not None:
            request.rows.append(contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:  # noqa: N802, N803
        request = self._lookup(reqId)
        if request is not None:
            request.done.set()

    def historicalData(self, reqId: int, bar: Any) -> None:  # noqa: N802, N803
        request = self._lookup(reqId)
        if request is not None:
            request.rows.append(bar)

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:  # noqa: N802, N803
        request = self._lookup(reqId)
        if request is not None:
            request.done.set()

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib: Any = None) -> None:  # noqa: N802, N803
        field = _PRICE_TICKS.get(tickType)
        ticker = self._tick(reqId) if field else None
        if ticker is not None:
            # -1 on a price tick means "no quote", not a price of minus one.
            ticker.set(field, _optional_float(price, allow_negative=False),
                       delayed=tickType in _DELAYED_TICKS)

    def tickSize(self, reqId: int, tickType: int, size: Any) -> None:  # noqa: N802, N803
        field = _SIZE_TICKS.get(tickType)
        ticker = self._tick(reqId) if field else None
        if ticker is not None:
            ticker.set(field, _optional_float(size, allow_negative=False),
                       delayed=tickType in _DELAYED_TICKS)

    def tickOptionComputation(  # noqa: N802
        self,
        reqId: int,  # noqa: N803
        tickType: int,  # noqa: N803
        tickAttrib: int,  # noqa: N803
        impliedVol: float,  # noqa: N803
        delta: float,
        optPrice: float,  # noqa: N803
        pvDividend: float,  # noqa: N803
        gamma: float,
        vega: float,
        theta: float,
        undPrice: float,  # noqa: N803
    ) -> None:
        """Greeks for one side. Only the model computation is kept.

        The bid and ask computations describe one side of the market; the model
        tick is TWS's own valuation and is the one field set that stays
        populated when a side is missing, which for a 0DTE wing is most of the
        session.
        """
        if tickType not in _MODEL_OPTION_TICKS:
            return
        ticker = self._tick(reqId)
        if ticker is None:
            return
        delayed = tickType in _DELAYED_TICKS
        ticker.set("iv", _optional_float(impliedVol, allow_negative=False), delayed=delayed)
        ticker.set("delta", _optional_float(delta), delayed=delayed)
        ticker.set("gamma", _optional_float(gamma), delayed=delayed)
        ticker.set("vega", _optional_float(vega), delayed=delayed)
        ticker.set("theta", _optional_float(theta), delayed=delayed)
        ticker.set("underlying", _optional_float(undPrice, allow_negative=False), delayed=delayed)

    def marketDataType(self, reqId: int, marketDataType: int) -> None:  # noqa: N802, N803
        """TWS reports which data it actually served. 3 and 4 are delayed."""
        ticker = self._tick(reqId)
        if ticker is not None and marketDataType >= 3:
            ticker.delayed = True

    def error(self, reqId: int, *args: Any) -> None:  # noqa: N802, N803
        """Handle TWS errors.

        The callback gained a leading `errorTime` field in TWS API 10.30, so the
        arguments are located relative to the message rather than by a fixed
        position: `errorCode` is always the int immediately before `errorString`.
        """
        code, message = _split_error_args(args)

        if _INFO_CODE_MIN <= code <= _INFO_CODE_MAX:
            logger.info("TWS %s: %s", code, message)
            return

        if code in MARKET_DATA_NOTICE_CODES:
            logger.info("TWS %s on request %s: %s", code, reqId, message)
            ticker = self._tick(reqId)
            if ticker is not None:
                ticker.error = IBKRError(code, message, reqId)
            return

        logger.warning("TWS error %s on request %s: %s", code, reqId, message)
        request = self._lookup(reqId)
        if request is not None:
            request.error = IBKRError(code, message, reqId)
            request.done.set()
            return

        # A subscription has no blocking caller to raise into, so the error is
        # recorded on the ticker and surfaces on the next `quotes()` read.
        ticker = self._tick(reqId)
        if ticker is not None:
            ticker.error = IBKRError(code, message, reqId)


def _split_error_args(args: tuple[Any, ...]) -> tuple[int, str]:
    """Pull `(errorCode, errorString)` out of the `error` callback arguments.

    TWS API 10.30 inserted `errorTime` before `errorCode`, so both layouts are
    in the wild:

        (errorCode, errorString, advancedOrderRejectJson)             # <= 10.29
        (errorTime, errorCode, errorString, advancedOrderRejectJson)  # >= 10.30

    In both, `errorCode` is the integer directly before the message.
    """
    message_at = next((i for i, a in enumerate(args) if isinstance(a, str)), None)
    if message_at is None:
        code = next((a for a in reversed(args) if isinstance(a, int)), -1)
        return code, ""
    message = args[message_at]
    before = args[message_at - 1] if message_at > 0 else None
    return (before if isinstance(before, int) else -1), message


def _as_bar(raw: Any) -> Bar:
    return Bar(
        ts=parse_bar_timestamp(raw.date),
        open=_to_float(raw.open),
        high=_to_float(raw.high),
        low=_to_float(raw.low),
        close=_to_float(raw.close),
        volume=_to_float(raw.volume),
        wap=_to_float(raw.wap),
        count=int(getattr(raw, "barCount", 0) or 0),
    )


def _as_quote(ticker: _Ticker) -> Quote:
    c = ticker.contract
    return Quote(
        con_id=int(c.conId or 0),
        local_symbol=c.localSymbol,
        right=c.right,
        strike=_to_float(c.strike),
        expiry=c.lastTradeDateOrContractMonth,
        ts=ticker.ts,
        delayed=ticker.delayed,
        error=ticker.error.message if ticker.error is not None else None,
        error_code=ticker.error.code if ticker.error is not None else None,
        **{k: v for k, v in ticker.values.items()},
    )


def _as_contract_detail(raw: ContractDetails) -> ContractDetail:
    c = raw.contract
    return ContractDetail(
        con_id=int(c.conId),
        local_symbol=c.localSymbol,
        symbol=c.symbol,
        sec_type=c.secType,
        exchange=c.exchange,
        currency=c.currency,
        last_trade_date=c.lastTradeDateOrContractMonth,
        strike=_to_float(c.strike),
        right=c.right,
        multiplier=c.multiplier,
        trading_hours=raw.tradingHours,
    )
