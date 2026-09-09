"""Roadmap step 2b: measure what live market data this account actually gives.

Two numbers decide the recorder's shape and neither is knowable from the docs:

  1. Does `reqMktData` on an SPXW 0DTE contract return **live** quotes, or does
     TWS fall back to delayed data? A recorder writing delayed quotes as if they
     were live would poison every spread and every residual downstream.
  2. How many concurrent market-data lines does the account have? That number
     is the width of the strike window the recorder can hold open at once.

This script only reads. It subscribes, measures, and cancels every subscription
before exiting. It may be pointed at a live account, so nothing here writes.

Usage:
    .venv/bin/python scripts/spike_marketdata.py
    IBKR_PORT=4001 .venv/bin/python scripts/spike_marketdata.py --lines 300
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from spx0dte.config import IBKRConfig
from spx0dte.ingest.contracts import spx_index, spxw_option
from spx0dte.ingest.ibkr_adapter import (
    MARKET_DATA_NOTICE_CODES,
    MAX_TICKERS_CODE,
    IBKRAdapter,
    IBKRError,
    IBKRTimeoutError,
    Quote,
)

logger = logging.getLogger("spike")

# TWS drops the connection past ~50 client messages per second. Subscriptions
# are sent one message each, so they are spaced rather than fired in a burst.
_SUBSCRIBE_GAP = 0.05


@dataclass
class Check:
    """One measured question and its answer."""

    name: str
    outcome: str
    detail: str


def _run(name: str, fn) -> Check:
    """Run one check, turning any TWS failure into a recorded outcome."""
    started = time.monotonic()
    try:
        outcome, detail = fn()
    except IBKRError as exc:
        outcome, detail = "TWS ERROR", f"code {exc.code}: {exc.message}"
    except IBKRTimeoutError as exc:
        outcome, detail = "TIMEOUT", str(exc)
    elapsed = time.monotonic() - started
    logger.info("%-34s %-18s %s  (%.1fs)", name, outcome, detail, elapsed)
    return Check(name, outcome, detail)


def atm_strike(api: IBKRAdapter) -> float:
    """Round the last SPX index close onto the 5-point strike grid."""
    bars = api.historical_bars(
        spx_index(), duration="2 D", bar_size="1 hour", what_to_show="TRADES"
    )
    if not bars:
        raise IBKRTimeoutError("no SPX index bars; cannot locate the ATM strike")
    return round(bars[-1].close / 5) * 5


def strike_ladder(centre: float, count: int, strikes: list[float]) -> list[tuple[float, str]]:
    """Listed strikes outwards from ATM, both rights, in the order a recorder wants.

    Nearest first: if the line budget runs out, what is lost is the far wing and
    not the money. Only strikes TWS actually lists are used — the grid widens
    from 5 points near the money to 25 in the wings, so a synthetic ladder
    spends lines on contracts that do not exist (error 200).
    """
    out: list[tuple[float, str]] = []
    for strike in sorted(strikes, key=lambda k: abs(k - centre)):
        for right in ("C", "P"):
            out.append((strike, right))
        if len(out) >= count:
            break
    return out[:count]


def check_live_quote(api: IBKRAdapter, expiry: str, strike: float,
                     settle: float) -> tuple[str, str]:
    """Assumption 1. One ATM call: is anything quoted, and is it live?"""
    req_id = api.subscribe(spxw_option(expiry, strike, "C"))
    try:
        quote = _wait_for(api, req_id, settle)
        if quote.error_code is not None and quote.error_code not in MARKET_DATA_NOTICE_CODES:
            return "NO DATA", f"code {quote.error_code}: {quote.error}"
        if quote.ts is None:
            return "SILENT", f"no tick within {settle:g}s for {expiry} {strike:.0f}C"
        kind = "DELAYED" if quote.delayed else "LIVE"
        return kind, _describe(quote)
    finally:
        api.unsubscribe(req_id)


def check_index_quote(api: IBKRAdapter, settle: float) -> tuple[str, str]:
    """Where does spot come from? The greeks arrive with `undPrice` unset.

    If the index itself is not entitled either, the recorder has to derive spot
    from the chain (put-call parity at the money) rather than read it.
    """
    req_id = api.subscribe(spx_index(), generic_ticks="")
    try:
        quote = _wait_for(api, req_id, settle)
        if quote.last is None and not quote.is_two_sided:
            code = quote.error_code
            return "NO DATA", f"code {code}: {quote.error}" if code else "no tick for SPX index"
        kind = "DELAYED" if quote.delayed else "LIVE"
        return kind, _describe(quote)
    finally:
        api.unsubscribe(req_id)


def check_line_budget(api: IBKRAdapter, expiry: str, centre: float, limit: int,
                      step: int, settle: float, strikes: list[float]) -> tuple[str, str]:
    """Assumption 2. Subscribe outwards from ATM until TWS refuses.

    Error 101 ("max number of tickers") is the wall. It arrives per rejected
    subscription, so every batch is inspected rather than only the last quote.
    """
    ladder = strike_ladder(centre, limit, strikes)
    opened: list[int] = []
    refused_at: str | None = None

    for start in range(0, len(ladder), step):
        for strike, right in ladder[start:start + step]:
            opened.append(api.subscribe(spxw_option(expiry, strike, right)))
            time.sleep(_SUBSCRIBE_GAP)
        time.sleep(settle)

        rejected = [q for q in api.quotes().values() if q.error_code == MAX_TICKERS_CODE]
        if rejected:
            refused_at = f"{len(opened)} lines requested, {len(rejected)} refused"
            break

    quotes = list(api.quotes().values())
    live = [q for q in quotes if q.ts is not None]
    noticed = [q for q in quotes if q.error_code in MARKET_DATA_NOTICE_CODES]
    detail = (f"{len(opened)} requested, {len(live)} receiving data"
              f", {len(noticed)} with an entitlement notice")
    if refused_at:
        return f"CAPPED AT {len(live)}", f"{detail}; {refused_at}"
    return "NO CAP HIT", f"{detail}; budget is at least {len(live)}"


def check_field_coverage(quotes: list[Quote]) -> tuple[str, str]:
    """What fraction of a wide subscription is actually usable for a snapshot?"""
    total = len(quotes)
    if not total:
        return "EMPTY", "nothing subscribed"

    def pct(predicate) -> str:
        return f"{100 * sum(1 for q in quotes if predicate(q)) / total:.0f}%"

    return "OK", (
        f"n={total}  two-sided {pct(lambda q: q.is_two_sided)}  "
        f"iv {pct(lambda q: q.iv is not None)}  "
        f"greeks {pct(lambda q: q.delta is not None)}  "
        f"OI {pct(lambda q: q.open_interest is not None)}  "
        f"underlying {pct(lambda q: q.underlying is not None)}  "
        f"vol {pct(lambda q: q.volume is not None)}  "
        f"delayed {pct(lambda q: q.delayed)}"
    )


def _wait_for(api: IBKRAdapter, req_id: int, settle: float) -> Quote:
    """Poll one subscription until it is two-sided, errored, or time runs out."""
    deadline = time.monotonic() + settle
    quote = api.quote(req_id)
    while time.monotonic() < deadline:
        quote = api.quote(req_id)
        if quote is None:
            break
        # An entitlement notice arrives before the ticks it does not block, so
        # only a real error ends the wait early.
        fatal = quote.error_code is not None and quote.error_code not in MARKET_DATA_NOTICE_CODES
        if fatal or quote.is_two_sided:
            return quote
        time.sleep(0.25)
    return quote if quote is not None else Quote(0, "", "", 0.0, "")


def _describe(quote: Quote) -> str:
    def num(value: float | None, fmt: str = ".2f") -> str:
        return "-" if value is None else format(value, fmt)

    return (
        f"bid {num(quote.bid)} x{num(quote.bid_size, '.0f')} / "
        f"ask {num(quote.ask)} x{num(quote.ask_size, '.0f')}  "
        f"iv {num(quote.iv, '.4f')}  delta {num(quote.delta, '.3f')}  "
        f"OI {num(quote.open_interest, '.0f')}  vol {num(quote.volume, '.0f')}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lines", type=int, default=200,
                        help="maximum subscriptions to attempt (0 to skip the budget check)")
    parser.add_argument("--step", type=int, default=20,
                        help="subscriptions opened between checks for a refusal")
    parser.add_argument("--settle", type=float, default=6.0,
                        help="seconds to wait for ticks after each batch")
    parser.add_argument("--expiry", default=None, help="YYYYMMDD; default is today UTC")
    parser.add_argument("--strike", type=float, default=None,
                        help="ATM strike; default is the SPX close rounded to 5")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for noisy in ("ibapi", "ibapi.client", "ibapi.wrapper", "ibapi.decoder", "ibapi.reader"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    config = IBKRConfig.from_env()
    label = "PAPER" if config.is_paper else "LIVE"
    logger.info("Connecting to %s:%s as client %s [%s] — read-only\n",
                config.host, config.port, config.client_id, label)

    expiry = args.expiry or f"{datetime.now(UTC):%Y%m%d}"
    checks: list[Check] = []

    api = IBKRAdapter(config, timeout=45.0)
    try:
        api.open()
    except IBKRTimeoutError as exc:
        logger.error("Could not connect: %s", exc)
        return 1

    try:
        # 1 = live. TWS silently serves delayed data when live is unavailable,
        # which is exactly what check 1 is here to detect.
        api.set_market_data_type(1)
        centre = args.strike if args.strike is not None else atm_strike(api)
        logger.info("expiry %s, ATM %.0f\n", expiry, centre)

        checks.append(_run(f"live quote, {expiry} {centre:.0f}C",
                           lambda: check_live_quote(api, expiry, centre, args.settle)))
        checks.append(_run("SPX index streaming quote",
                           lambda: check_index_quote(api, args.settle)))

        if args.lines:
            listed = sorted({d.strike for d in api.contract_details(spxw_option(expiry))})
            logger.info("chain lists %d strikes, %.0f-%.0f\n",
                        len(listed), listed[0], listed[-1])
            budget = _run(
                f"line budget (up to {args.lines})",
                lambda: check_line_budget(api, expiry, centre, args.lines,
                                          args.step, args.settle, listed),
            )
            checks.append(budget)
            checks.append(_run("field coverage",
                               lambda: check_field_coverage(list(api.quotes().values()))))
    finally:
        api.unsubscribe_all()
        api.close()

    print("\n" + "=" * 100)
    print(f"{'check':<38} {'outcome':<18} detail")
    print("-" * 100)
    for c in checks:
        print(f"{c.name:<38} {c.outcome:<18} {c.detail}")
    print("=" * 100)
    print("\nRecord these numbers in DECISIONS.md before building the recorder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
