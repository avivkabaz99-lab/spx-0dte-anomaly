"""Record the live SPXW 0DTE chain to Parquet.

IBKR keeps an expired option chain for one trading day (see DECISIONS.md), so
every session that is not recorded is gone. This module is the recorder.

Shape of the thing, and why:

- The whole chain is subscribed at once. The measured line budget is at least
  484, which is the entire expiry, so there is no ATM window to maintain. If TWS
  ever refuses with error 101 the subscription order does the fallback for free:
  strikes are opened nearest-the-money first, so what is lost is the far wing.
- Sampling costs no requests. `reqMktData` streams, the adapter keeps the last
  tick per contract, and a sample is a local read. The ~60 requests / 10 min
  historical limit therefore does not apply to this loop at all.
- Spot is the one field this account cannot get live. The SPX index is not
  entitled for streaming and `undPrice` never arrives on the greeks, so spot is
  polled from a historical bar that runs ~16 minutes behind the tape. It is
  written with its own `spot_ts` and is never passed off as current: anything
  needing a spot at quote time recovers it from put-call parity on the recorded
  chain, which is entitled, live, and stored at every strike.
- Missing stays missing. A one-sided wing writes null, never 0.0.

Raw quotes stay on this machine: `data/` and `*.parquet` are gitignored, and
nothing here writes to Postgres.

Usage:
    IBKR_PORT=4001 .venv/bin/python -m spx0dte.ingest.recorder
    IBKR_PORT=4001 .venv/bin/python -m spx0dte.ingest.recorder --until 16:15
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
from ibapi.contract import Contract

from spx0dte.config import IBKRConfig
from spx0dte.ingest.contracts import spx_index, spxw_option
from spx0dte.ingest.ibkr_adapter import (
    MARKET_DATA_NOTICE_CODES,
    MAX_TICKERS_CODE,
    ContractDetail,
    IBKRAdapter,
    IBKRError,
    IBKRTimeoutError,
    Quote,
)

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")

# SPXW is PM-settled and trades until 16:15 ET, a quarter hour past the equity
# close. The last minutes are the most interesting ones for 0DTE.
SESSION_END = clock_time(16, 15)

# TWS drops a client that exceeds ~50 messages per second. One subscription is
# one message, and the chain is opened in a single pass.
SUBSCRIBE_GAP = 0.05

# `undPrice` never arrives and the index is not entitled for streaming, so spot
# comes from a historical bar. That feed is *delayed by about 16 minutes* on
# this account, which dictates the window: a request only answers if it reaches
# back further than the delay. A "60 S" window can therefore never contain a
# bar, and "1 D" is empty for the first ~20 minutes of a session because it is
# scoped to a day the delayed feed has not reached yet. "2 D" always spans the
# lag and the weekend, and 1 min is the finest bar the account does serve.
SPOT_DURATION = "2 D"
SPOT_BAR_SIZE = "1 min"

# Bars are a minute wide, so polling faster buys nothing. One request a minute
# is also well inside the ~60 requests / 10 min historical limit.
SPOT_MAX_AGE = 60.0

# Spot trails the tape by the feed's ~16 min delay, so that much age is normal
# and must not warn. Past this it means the feed stopped, which the operator
# needs to hear. Before the open it is legitimately hours old and says so.
SPOT_STALE_AFTER = 1800.0

SCHEMA = pa.schema([
    ("ts", pa.timestamp("us", tz="UTC")),
    ("con_id", pa.int64()),
    ("local_symbol", pa.string()),
    ("expiry", pa.string()),
    ("strike", pa.float64()),
    ("right", pa.string()),
    ("bid", pa.float64()),
    ("bid_size", pa.float64()),
    ("ask", pa.float64()),
    ("ask_size", pa.float64()),
    ("last", pa.float64()),
    ("last_size", pa.float64()),
    ("volume", pa.float64()),
    ("open_interest", pa.float64()),
    ("iv", pa.float64()),
    ("delta", pa.float64()),
    ("gamma", pa.float64()),
    ("vega", pa.float64()),
    ("theta", pa.float64()),
    ("quote_ts", pa.timestamp("us", tz="UTC")),
    ("spot", pa.float64()),
    ("spot_ts", pa.timestamp("us", tz="UTC")),
    ("delayed", pa.bool_()),
    ("error_code", pa.int32()),
])


def today_expiry(now: datetime | None = None) -> str:
    """Today's expiry in exchange time. UTC would roll the date at 20:00 ET."""
    moment = (now or datetime.now(UTC)).astimezone(EASTERN)
    return f"{moment:%Y%m%d}"


def session_end(now: datetime | None = None, end: clock_time = SESSION_END) -> datetime:
    """The UTC instant SPXW stops trading on the current exchange day."""
    moment = (now or datetime.now(UTC)).astimezone(EASTERN)
    return moment.replace(
        hour=end.hour, minute=end.minute, second=0, microsecond=0
    ).astimezone(UTC)


def contract_from(detail: ContractDetail) -> Contract:
    """Rebuild a subscribable contract from a resolved one.

    TWS resolves on `conId` alone and ignores the rest, but the adapter reads
    strike, right and expiry back off the contract it was handed, so a
    conId-only contract records a chain of blank strikes. Every field here comes
    from `reqContractDetails`, so it cannot disagree with what TWS resolves.
    """
    contract = Contract()
    contract.conId = detail.con_id
    contract.exchange = detail.exchange or "SMART"
    contract.symbol = detail.symbol
    contract.secType = detail.sec_type
    contract.currency = detail.currency
    contract.localSymbol = detail.local_symbol
    contract.lastTradeDateOrContractMonth = detail.last_trade_date
    contract.strike = detail.strike
    contract.right = detail.right
    contract.multiplier = detail.multiplier
    return contract


def chain(api: IBKRAdapter, expiry: str) -> list[ContractDetail]:
    """Every listed SPXW contract for one expiry, both rights."""
    details: list[ContractDetail] = []
    for right in ("C", "P"):
        details.extend(api.contract_details(spxw_option(expiry, right=right)))
    return details


def atm_first(details: Iterable[ContractDetail], centre: float) -> list[ContractDetail]:
    """Order the chain outwards from the money.

    This is the whole line-budget fallback: subscriptions are opened in this
    order, so a refusal costs the far wing rather than the strikes that matter.
    """
    return sorted(details, key=lambda d: (abs(d.strike - centre), d.strike, d.right))


class SpotPoll:
    """Last SPX print, refreshed from a one-minute historical bar on demand.

    A failed poll keeps the previous value and its original timestamp, so a
    stale spot is visible in the data rather than silently carried forward as
    if it were current.
    """

    def __init__(self, api: IBKRAdapter, max_age: float = SPOT_MAX_AGE) -> None:
        self._api = api
        self._max_age = max_age
        self._value: float | None = None
        self._ts: datetime | None = None
        self._fetched = 0.0
        self._warned = 0.0

    def value(self) -> tuple[float | None, datetime | None]:
        """Spot and the time of the bar it came from, refreshing if stale."""
        if self._value is not None and time.monotonic() - self._fetched < self._max_age:
            return self._value, self._ts
        try:
            bars = self._api.historical_bars(
                spx_index(), duration=SPOT_DURATION, bar_size=SPOT_BAR_SIZE,
                what_to_show="TRADES", use_rth=True,
            )
        except (IBKRError, IBKRTimeoutError) as exc:
            logger.warning("spot poll failed, keeping %s: %s", self._value, exc)
            return self._value, self._ts
        self._fetched = time.monotonic()
        if bars:
            self._value, self._ts = bars[-1].close, bars[-1].ts
            age = (datetime.now(UTC) - self._ts).total_seconds()
            # Before the open this is true on every poll for hours, so it is
            # said once in a while rather than once a sample.
            if age > SPOT_STALE_AFTER and self._fetched - self._warned > SPOT_STALE_AFTER:
                self._warned = self._fetched
                logger.warning("spot is %.0f min old (%s); the index is not printing",
                               age / 60, self._ts)
        return self._value, self._ts


def rows_from_quotes(
    quotes: Iterable[Quote],
    spot: float | None,
    spot_ts: datetime | None,
    ts: datetime,
) -> list[dict[str, object]]:
    """One row per contract. Every unset field stays null."""
    return [
        {
            "ts": ts,
            "con_id": quote.con_id,
            "local_symbol": quote.local_symbol,
            "expiry": quote.expiry,
            "strike": quote.strike,
            "right": quote.right,
            "bid": quote.bid,
            "bid_size": quote.bid_size,
            "ask": quote.ask,
            "ask_size": quote.ask_size,
            "last": quote.last,
            "last_size": quote.last_size,
            "volume": quote.volume,
            "open_interest": quote.open_interest,
            "iv": quote.iv,
            "delta": quote.delta,
            "gamma": quote.gamma,
            "vega": quote.vega,
            "theta": quote.theta,
            "quote_ts": quote.ts,
            "spot": spot,
            "spot_ts": spot_ts,
            "delayed": quote.delayed,
            # 10090 lands on every option subscription because the index is not
            # entitled. Writing it per row would bury a real per-contract error.
            "error_code": (None if quote.error_code in MARKET_DATA_NOTICE_CODES
                           else quote.error_code),
        }
        for quote in quotes
    ]


@dataclass
class ParquetSink:
    """Buffers rows and writes one part file per flush.

    Part files rather than one file per day: a flush every few minutes means a
    crash costs minutes, and an append-only directory needs no rewrite.
    """

    root: Path
    buffer: list[dict[str, object]] = field(default_factory=list)

    def add(self, rows: Sequence[dict[str, object]]) -> None:
        self.buffer.extend(rows)

    def flush(self, now: datetime | None = None) -> Path | None:
        """Write the buffer and return the file, or None when there is nothing."""
        if not self.buffer:
            return None
        moment = (now or datetime.now(UTC)).astimezone(EASTERN)
        directory = self.root / f"date={moment:%Y-%m-%d}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"part-{moment:%H%M%S}.parquet"

        table = pa.Table.from_pylist(self.buffer, schema=SCHEMA)
        pq.write_table(table, path, compression="zstd")
        logger.info("wrote %d rows to %s", len(self.buffer), path)
        self.buffer.clear()
        return path


class Recorder:
    """Subscribes a chain and samples it on a fixed interval."""

    def __init__(
        self,
        api: IBKRAdapter,
        sink: ParquetSink,
        *,
        interval: float = 30.0,
        flush_every: int = 10,
    ) -> None:
        self._api = api
        self._sink = sink
        self._interval = interval
        self._flush_every = flush_every
        self._spot = SpotPoll(api)
        self._stop = threading.Event()
        self.samples = 0
        # Samples deliberately not taken because the feed was down. Counted so
        # the gap is visible in the log and reported at the end, rather than
        # showing up months later as an unexplained hole in a backtest.
        self.skipped = 0

    def stop(self) -> None:
        """Ask the loop to finish after the current sample."""
        self._stop.set()

    def subscribe_chain(self, details: Sequence[ContractDetail], settle: float = 5.0) -> int:
        """Open one line per contract, nearest the money first.

        Returns the number of live subscriptions. Anything TWS refused with
        error 101 is dropped, which leaves exactly the widest window the account
        can hold.
        """
        for detail in details:
            self._api.subscribe(contract_from(detail))
            time.sleep(SUBSCRIBE_GAP)
        time.sleep(settle)

        refused = [rid for rid, q in self._api.quotes().items()
                   if q.error_code == MAX_TICKERS_CODE]
        for req_id in refused:
            self._api.unsubscribe(req_id)
        if refused:
            logger.warning(
                "line budget reached: %d of %d contracts refused, recording the "
                "%d nearest the money", len(refused), len(details), len(details) - len(refused),
            )
        return len(details) - len(refused)

    def sample(self, now: datetime | None = None) -> int:
        """Take one snapshot of every subscription. Returns the row count."""
        ts = now or datetime.now(UTC)
        spot, spot_ts = self._spot.value()
        rows = rows_from_quotes(self._api.quotes().values(), spot, spot_ts, ts)
        self._sink.add(rows)
        self.samples += 1
        if self.samples % self._flush_every == 0:
            self._sink.flush(ts)
        return len(rows)

    def run(self, until: datetime) -> None:
        """Sample until `until` or until stopped, then flush what is buffered.

        The deadline advances by a fixed interval rather than sleeping for one,
        so a slow sample steals from the next wait instead of drifting the whole
        session later and later.
        """
        deadline = time.monotonic()
        try:
            while not self._stop.is_set() and datetime.now(UTC) < until:
                # A dropped feed leaves the last tick of every contract in place,
                # so sampling on would write minutes of stale quotes that look
                # valid. Stopping lets the launchd agent start a fresh one.
                if not self._api.is_connected:
                    logger.error("TWS connection lost after %d samples; stopping", self.samples)
                    return

                # TWS restored the link but dropped the streams. Every
                # subscription would have to be re-issued, and a fresh process
                # does that from a known state; carrying on would record a chain
                # nothing is feeding.
                if self._api.subscriptions_lost:
                    logger.error("TWS 1101: subscriptions dropped after %d samples; "
                                 "stopping so a fresh recorder resubscribes", self.samples)
                    return

                # The link out to IBKR is down while the local socket is fine.
                # This recovers on its own, so the loop pauses rather than
                # exiting: exiting would cost a full 484-contract resubscription
                # and up to half an hour of launchd wait for an outage that
                # typically clears in minutes.
                if not self._api.market_data_ok:
                    self.skipped += 1
                    if self.skipped == 1 or self.skipped % 10 == 0:
                        logger.warning("market data feed is down; skipped %d sample(s), "
                                       "writing nothing until it returns", self.skipped)
                    deadline += self._interval
                    self._wait_until(deadline)
                    continue
                if self.skipped:
                    logger.info("market data feed is back after %d skipped sample(s)",
                                self.skipped)
                    self.skipped = 0
                started = time.monotonic()
                count = self.sample()
                logger.debug("sample %d: %d rows in %.2fs",
                             self.samples, count, time.monotonic() - started)
                deadline += self._interval
                self._wait_until(deadline)
        finally:
            self._sink.flush()

    def _wait_until(self, deadline: float) -> None:
        """Sleep in slices so a stop signal is not held up by the interval."""
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._stop.wait(min(remaining, 0.5))


def main() -> int:
    parser = argparse.ArgumentParser(description="Record the SPXW 0DTE chain to Parquet.")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between samples")
    parser.add_argument("--flush-every", type=int, default=10, help="samples per Parquet part")
    parser.add_argument("--until", default=None,
                        help="HH:MM in exchange time; default is the 16:15 ET SPXW close")
    parser.add_argument("--expiry", default=None, help="YYYYMMDD; default is today ET")
    parser.add_argument("--out", type=Path, default=Path("data/chains"),
                        help="root directory for the date= partitions")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    for noisy in ("ibapi", "ibapi.client", "ibapi.wrapper", "ibapi.decoder", "ibapi.reader"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    config = IBKRConfig.from_env()
    logger.info("connecting to %s:%s as client %s [%s] — read-only",
                config.host, config.port, config.client_id,
                "PAPER" if config.is_paper else "LIVE")

    expiry = args.expiry or today_expiry()
    if args.until:
        hour, minute = (int(part) for part in args.until.split(":"))
        until = session_end(end=clock_time(hour, minute))
    else:
        until = session_end()

    api = IBKRAdapter(config, timeout=20.0)
    try:
        api.open()
    except IBKRTimeoutError as exc:
        logger.error("could not connect: %s", exc)
        return 1

    recorder = Recorder(api, ParquetSink(args.out),
                        interval=args.interval, flush_every=args.flush_every)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: recorder.stop())

    try:
        api.set_market_data_type(1)
        spot, _ = SpotPoll(api).value()
        if spot is None:
            logger.error("no SPX print available; cannot order the chain around the money")
            return 1

        details = atm_first(chain(api, expiry), spot)
        if not details:
            logger.error("no contracts listed for %s; is it a trading day?", expiry)
            return 1

        opened = recorder.subscribe_chain(details)
        logger.info(
            "recording %d contracts of expiry %s, spot %.2f, every %.0fs until %s ET",
            opened, expiry, spot, args.interval, f"{until.astimezone(EASTERN):%H:%M}",
        )
        recorder.run(until)
    finally:
        api.unsubscribe_all()
        api.close()

    logger.info("stopped after %d samples, %d skipped while the feed was down",
                recorder.samples, recorder.skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
