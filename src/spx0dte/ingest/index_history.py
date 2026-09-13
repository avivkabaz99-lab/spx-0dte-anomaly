"""Backfill 1-minute index bars from IBKR into the shared bars layout.

Module B trains on SPX minute bars plus the CBOE vol indices, and unlike the
0DTE chain these do have history at IBKR. What limits the backfill is pacing:
about 60 historical requests per 10 minutes, so this walks backwards in
`2 D` chunks (the window measured to work against this account's delayed
index feed, see DECISIONS.md) with a fixed gap between requests, and writes
one part per (symbol, Eastern date) so a second run only fetches what is
missing.

Read-only. Historical requests never touch an order. It runs beside the
recorder on its own client id.

Usage:
    IBKR_PORT=4001 .venv/bin/python -m spx0dte.ingest.index_history --probe
    IBKR_PORT=4001 .venv/bin/python -m spx0dte.ingest.index_history --days 126
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd

from spx0dte.config import IBKRConfig
from spx0dte.features.bars import COLUMNS, dates_on_disk, validate_bars, write_bars
from spx0dte.features.session import EASTERN
from spx0dte.ingest.contracts import cboe_index
from spx0dte.ingest.ibkr_adapter import Bar, IBKRAdapter, IBKRError, IBKRTimeoutError

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ("SPX", "VIX1D", "VIX9D", "VIX")
DEFAULT_CLIENT_ID = 12  # the recorder is 11 (IBKR_CLIENT_ID); never share one

# ~60 requests / 10 min is the documented historical limit; 10.5 s keeps a
# sequential walk under it with margin.
REQUEST_GAP = 10.5
PACING_BACKOFF = 60.0

CHUNK = "2 D"
BAR_SIZE = "1 min"
END_FORMAT = "%Y%m%d-%H:%M:%S"  # UTC, what the adapter's `end` expects


class SymbolUnavailable(RuntimeError):
    """TWS does not know the contract or does not serve its history."""


@dataclass(frozen=True)
class FetchPlan:
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    days: int = 126
    chunk: str = CHUNK
    include_today: bool = False
    force: bool = False


def bars_to_frame(bars: Sequence[Bar], symbol: str) -> pd.DataFrame:
    """Adapter bars to the shared schema. Empty input gives an empty frame."""
    if not bars:
        return pd.DataFrame({c: pd.Series(dtype="object") for c in COLUMNS})
    frame = pd.DataFrame({
        "ts": pd.to_datetime([b.ts for b in bars], utc=True).as_unit("us"),
        "symbol": symbol,
        "open": [b.open for b in bars],
        "high": [b.high for b in bars],
        "low": [b.low for b in bars],
        "close": [b.close for b in bars],
        "volume": [b.volume for b in bars],
    })
    return validate_bars(frame)


def _is_pacing(exc: IBKRError) -> bool:
    return "pacing" in exc.message.lower() or exc.code in (165, 420)


def _is_no_data(exc: IBKRError) -> bool:
    return exc.code == 162 and "no data" in exc.message.lower()


def request_bars(
    api: IBKRAdapter,
    symbol: str,
    *,
    end: str,
    duration: str,
    sleep: Callable[[float], None],
) -> list[Bar]:
    """One historical request with the error branching DECISIONS.md documents.

    Pacing → back off once and retry. "No data" → an empty answer. A contract
    or entitlement refusal (200, 354, 10167...) → `SymbolUnavailable`, so the
    caller can move on to the next symbol.
    """
    for attempt in (1, 2):
        try:
            return api.historical_bars(
                cboe_index(symbol), end=end, duration=duration, bar_size=BAR_SIZE,
                what_to_show="TRADES", use_rth=True,
            )
        except IBKRError as exc:
            if _is_pacing(exc) and attempt == 1:
                logger.warning("%s: pacing pushback, sleeping %.0fs", symbol, PACING_BACKOFF)
                sleep(PACING_BACKOFF)
                continue
            if _is_no_data(exc):
                return []
            raise SymbolUnavailable(f"{symbol}: TWS error {exc.code}: {exc.message}") from exc
    raise SymbolUnavailable(f"{symbol}: still throttled after backoff")


def fetch_symbol(
    api: IBKRAdapter,
    symbol: str,
    plan: FetchPlan,
    root: Path,
    *,
    sleep: Callable[[float], None] = time.sleep,
    today: date | None = None,
) -> int:
    """Walk backwards from now until `plan.days` sessions exist on disk. Returns dates written."""
    today = today or datetime.now(UTC).astimezone(EASTERN).date()
    have = set() if plan.force else dates_on_disk(root, symbol)
    written = 0
    end = ""
    empty_streak = 0
    max_requests = plan.days * 2 + 5  # guard against a walk that never ends

    for _ in range(max_requests):
        if len(have) >= plan.days:
            break
        bars = request_bars(api, symbol, end=end, duration=plan.chunk, sleep=sleep)
        if not bars:
            empty_streak += 1
            if empty_streak >= 2:
                logger.info("%s: two empty answers in a row, stopping at %s", symbol, end or "now")
                break
            end = _step_back(end)
            sleep(REQUEST_GAP)
            continue
        empty_streak = 0
        frame = bars_to_frame(bars, symbol)
        frame = frame.assign(day=frame["ts"].dt.tz_convert(EASTERN).dt.date)
        oldest = frame["ts"].min()
        keep = ~frame["day"].isin(have)
        if not plan.include_today:
            keep &= frame["day"] != today
        fresh = frame[keep].drop(columns="day")
        if not fresh.empty:
            write_bars(fresh, root)
            new_days = set(fresh["ts"].dt.tz_convert(EASTERN).dt.date)
            written += len(new_days)
            have |= new_days
        end = (oldest - timedelta(seconds=1)).strftime(END_FORMAT)
        sleep(REQUEST_GAP)
    logger.info("%s: %d sessions on disk, %d written this run", symbol, len(have), written)
    return written


def _step_back(end: str) -> str:
    """When an answer is empty, move the window back a day rather than repeat it."""
    if not end:
        moment = datetime.now(UTC)
    else:
        moment = datetime.strptime(end, END_FORMAT).replace(tzinfo=UTC)
    return (moment - timedelta(days=1)).strftime(END_FORMAT)


def probe(
    api: IBKRAdapter, symbols: Sequence[str], *, sleep: Callable[[float], None] = time.sleep
) -> dict[str, str]:
    """One request per symbol: does this account serve its 1-min history at all?"""
    report: dict[str, str] = {}
    for symbol in symbols:
        try:
            bars = request_bars(api, symbol, end="", duration=CHUNK, sleep=sleep)
        except SymbolUnavailable as exc:
            report[symbol] = f"UNAVAILABLE — {exc}"
        except IBKRTimeoutError:
            report[symbol] = "TIMEOUT"
        else:
            if bars:
                newest = max(b.ts for b in bars)
                age = (datetime.now(UTC) - newest).total_seconds() / 60
                report[symbol] = (
                    f"{len(bars)} bars, newest {newest:%Y-%m-%d %H:%M}Z ({age:.0f} min old)"
                )
            else:
                report[symbol] = "0 bars (no data in window)"
        logger.info("probe %s: %s", symbol, report[symbol])
        sleep(REQUEST_GAP)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill 1-min index bars from IBKR.")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--days", type=int, default=126, help="sessions wanted per symbol")
    parser.add_argument("--chunk", default=CHUNK, help="IBKR duration per request")
    parser.add_argument("--out", type=Path, default=Path("data/bars"))
    parser.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID)
    parser.add_argument("--probe", action="store_true", help="one request per symbol, no writes")
    parser.add_argument("--force", action="store_true", help="rewrite dates already on disk")
    parser.add_argument("--include-today", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    for name in ("ibapi", "ibapi.client", "ibapi.wrapper", "ibapi.decoder", "ibapi.reader"):
        logging.getLogger(name).setLevel(logging.WARNING)

    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())
    config = dataclasses.replace(IBKRConfig.from_env(), client_id=args.client_id)
    logger.info(
        "connecting to %s:%s as client %s [%s] — read-only, historical only",
        config.host, config.port, config.client_id, "PAPER" if config.is_paper else "LIVE",
    )
    api = IBKRAdapter(config, timeout=30.0)
    try:
        api.open()
    except IBKRTimeoutError as exc:
        logger.error("could not connect: %s", exc)
        return 1
    try:
        if args.probe:
            probe(api, symbols)
            return 0
        plan = FetchPlan(symbols=symbols, days=args.days, chunk=args.chunk,
                         include_today=args.include_today, force=args.force)
        for symbol in symbols:
            try:
                fetch_symbol(api, symbol, plan, args.out)
            except SymbolUnavailable as exc:
                logger.error("%s", exc)
    finally:
        api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
