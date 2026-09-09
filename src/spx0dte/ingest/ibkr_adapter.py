"""Read-only adapter over IBKR's callback-based `ibapi` client.

`ibapi` delivers every result through `EWrapper` callbacks on a reader thread.
This adapter turns that into blocking calls that return plain dataclasses, so
nothing downstream has to know callbacks exist.

The adapter is deliberately read-only. It wraps contract lookup and historical
bars only, and `placeOrder` is overridden to raise. See the hard rules in
CLAUDE.md: the IBKR connection never places an order.
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


class _Request:
    """Mutable state for one in-flight request, resolved by callbacks."""

    def __init__(self, req_id: int) -> None:
        self.req_id = req_id
        self.done = threading.Event()
        self.rows: list[Any] = []
        self.error: IBKRError | None = None


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
        """Disconnect and let the reader thread finish."""
        try:
            self.disconnect()
        finally:
            if self._reader is not None:
                self._reader.join(timeout=5.0)
                self._reader = None

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

        logger.warning("TWS error %s on request %s: %s", code, reqId, message)
        request = self._lookup(reqId)
        if request is not None:
            request.error = IBKRError(code, message, reqId)
            request.done.set()


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
