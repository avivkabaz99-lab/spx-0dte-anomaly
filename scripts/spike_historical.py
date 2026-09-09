"""Roadmap step 1: measure what IBKR historical data actually gives us for 0DTE.

Two assumptions in DECISIONS.md order the whole roadmap and have never been
measured:

  1. `includeExpired` is documented as futures-only, so an expired SPXW option
     is not queryable. If that is wrong, Module A can be trained on history and
     the B-before-A ordering may flip.
  2. Historical pacing (~60 requests / 10 min) makes bulk chain history
     impractical.

This script only reads. It never places an order, and the adapter it uses
refuses to. It may be pointed at a live account, so nothing here writes.

Usage:
    .venv/bin/python scripts/spike_historical.py            # all checks
    .venv/bin/python scripts/spike_historical.py --pacing 20
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ibapi.contract import Contract

from spx0dte.config import IBKRConfig
from spx0dte.ingest.ibkr_adapter import IBKRAdapter, IBKRError, IBKRTimeoutError

logger = logging.getLogger("spike")


def spx_index() -> Contract:
    """The SPX index itself, used to locate the at-the-money strike."""
    c = Contract()
    c.symbol = "SPX"
    c.secType = "IND"
    c.exchange = "CBOE"
    c.currency = "USD"
    return c


def spxw_option(
    expiry: str,
    strike: float | None = None,
    right: str = "C",
    *,
    include_expired: bool = False,
) -> Contract:
    """One SPXW contract, or the whole expiry when `strike` is None.

    Leave `strike` as None to list a chain. Do not pass 0.0 for that: `ibapi`
    initialises strike to an UNSET sentinel, and writing a real 0.0 turns it
    into a filter that matches no contract (TWS answers error 200).
    """
    c = Contract()
    c.symbol = "SPX"
    c.secType = "OPT"
    c.exchange = "SMART"
    c.currency = "USD"
    c.tradingClass = "SPXW"
    c.lastTradeDateOrContractMonth = expiry
    if strike is not None:
        c.strike = strike
    c.right = right
    c.multiplier = "100"
    c.includeExpired = include_expired
    return c


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
    logger.info("%-34s %-12s %s  (%.1fs)", name, outcome, detail, elapsed)
    return Check(name, outcome, detail)


def check_index_history(api: IBKRAdapter) -> tuple[str, str]:
    """Baseline: does anything at all come back? Isolates auth/subscription."""
    bars = api.historical_bars(
        spx_index(), duration="2 D", bar_size="1 hour", what_to_show="TRADES"
    )
    if not bars:
        return "EMPTY", "no bars for the SPX index"
    last = bars[-1]
    return "OK", f"{len(bars)} bars, last close {last.close:.2f} at {last.ts:%Y-%m-%d %H:%M}"


def check_live_chain_resolves(api: IBKRAdapter, expiry: str) -> tuple[str, str]:
    """Can today's SPXW expiry be resolved at all, outside trading hours?"""
    rows = api.contract_details(spxw_option(expiry))
    if not rows:
        return "EMPTY", f"no SPXW contracts resolved for {expiry}"
    strikes = sorted({r.strike for r in rows})
    return "OK", f"{len(rows)} contracts, {len(strikes)} strikes {strikes[0]:.0f}-{strikes[-1]:.0f}"


def check_expired_contract(api: IBKRAdapter, expiry: str, strike: float) -> tuple[str, str]:
    """Assumption 1. Expected to fail; a success reorders the roadmap."""
    contract = spxw_option(expiry, strike, include_expired=True)
    rows = api.contract_details(contract)
    if not rows:
        return "NOT AVAILABLE", f"no contract resolved for expired {expiry} {strike:.0f}C"
    bars = api.historical_bars(contract, duration="1 D", bar_size="30 mins", what_to_show="TRADES")
    if not bars:
        return "RESOLVES, NO BARS", f"contract {rows[0].con_id} resolved but returned 0 bars"
    return "AVAILABLE", (
        f"{len(bars)} bars for expired {expiry} {strike:.0f}C — reread SPEC section 3"
    )


def check_retention_window(api: IBKRAdapter, today: datetime, back: int = 6) -> tuple[str, str]:
    """How many trading days of expired chains stay resolvable?

    This is the number that decides whether a missed recording day can be
    backfilled the next morning, or is lost for good.

    Every candidate day is probed; the walk does not stop at the first miss. A
    miss can be an exchange holiday rather than the end of the window, and
    `previous_weekday` only skips weekends. 2026-09-07 (Labor Day) is exactly
    that case.
    """
    day = today
    resolved: list[str] = []
    missing: list[str] = []

    for _ in range(back):
        day = previous_weekday(day)
        expiry = f"{day:%Y%m%d}"
        try:
            rows = api.contract_details(spxw_option(expiry, include_expired=True))
        except (IBKRError, IBKRTimeoutError):
            rows = []
        (resolved if rows else missing).append(expiry)

    if not resolved:
        return "NONE", f"no expired chain resolved; probed {', '.join(missing)}"
    return (
        f"{len(resolved)} OF {back} PROBED",
        f"resolved {', '.join(resolved)}; not resolved {', '.join(missing)} "
        "(a miss may be an exchange holiday)",
    )


def check_pacing(api: IBKRAdapter, expiry: str, centre: float, count: int) -> tuple[str, str]:
    """Assumption 2. Fire sequential requests until TWS actually pushes back.

    TWS overloads error 162 for both "pacing violation" and "query returned no
    data". Only the first is throttling; an empty answer is a completed request
    and still consumes a slot, so it counts toward the rate.
    """
    strikes = [centre + 5 * (i - count // 2) for i in range(count)]
    started = time.monotonic()
    completed = 0
    empty = 0
    pushback: str | None = None

    for strike in strikes:
        try:
            bars = api.historical_bars(
                spxw_option(expiry, strike, include_expired=True),
                duration="1 D",
                bar_size="30 mins",
                what_to_show="TRADES",
            )
            completed += 1
            if not bars:
                empty += 1
        except IBKRError as exc:
            text = exc.message.lower()
            if "pacing" in text or exc.code in (165, 420):
                pushback = f"code {exc.code} after {completed} requests: {exc.message[:70]}"
                break
            if exc.code in (162, 200, 300):
                # No data for this strike, or it does not exist. Still a round trip.
                completed += 1
                empty += 1
                continue
            pushback = f"unexpected code {exc.code} after {completed}: {exc.message[:70]}"
            break
        except IBKRTimeoutError:
            pushback = f"timeout after {completed} requests"
            break

    elapsed = time.monotonic() - started
    rate = completed / elapsed * 60 if elapsed else 0.0
    detail = f"{completed}/{count} round trips in {elapsed:.0f}s ({rate:.0f}/min), {empty} empty"
    if pushback:
        return "THROTTLED", f"{detail}; {pushback}"
    return "NO PUSHBACK", detail


def previous_weekday(day: datetime) -> datetime:
    out = day - timedelta(days=1)
    while out.weekday() >= 5:
        out -= timedelta(days=1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pacing", type=int, default=0,
                        help="number of sequential requests for the pacing check (0 to skip)")
    parser.add_argument("--strike", type=float, default=None,
                        help="strike for the expired-contract check; default is the SPX close")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # ibapi logs every decoded message at INFO, which buries the results.
    for noisy in ("ibapi", "ibapi.client", "ibapi.wrapper", "ibapi.decoder", "ibapi.reader"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    config = IBKRConfig.from_env()
    label = "PAPER" if config.is_paper else "LIVE"
    logger.info("Connecting to %s:%s as client %s [%s] — read-only\n",
                config.host, config.port, config.client_id, label)

    today = datetime.now(UTC)
    today_expiry = f"{today:%Y%m%d}"
    prev_expiry = f"{previous_weekday(today):%Y%m%d}"

    checks: list[Check] = []
    api = IBKRAdapter(config, timeout=45.0)
    try:
        api.open()
    except IBKRTimeoutError as exc:
        logger.error("Could not connect: %s", exc)
        return 1

    try:
        index = _run("index history (baseline)", lambda: check_index_history(api))
        checks.append(index)

        strike = args.strike
        if strike is None and index.outcome == "OK":
            # "last close 6543.21 at ..." -> 6545, rounded to the 5-point grid.
            close = float(index.detail.split("last close ")[1].split()[0])
            strike = round(close / 5) * 5
        strike = strike or 6500.0

        checks.append(_run(f"today's SPXW chain {today_expiry}",
                           lambda: check_live_chain_resolves(api, today_expiry)))
        checks.append(_run(f"expired SPXW {prev_expiry} {strike:.0f}C",
                           lambda: check_expired_contract(api, prev_expiry, strike)))
        checks.append(_run("expired-chain retention window",
                           lambda: check_retention_window(api, today)))

        if args.pacing:
            # Yesterday's expiry is used: today's 0DTE strikes have no trades
            # before the open, which would measure empty answers, not pacing.
            checks.append(_run(f"pacing, {args.pacing} sequential",
                               lambda: check_pacing(api, prev_expiry, strike, args.pacing)))
    finally:
        api.close()

    print("\n" + "=" * 78)
    print(f"{'check':<38} {'outcome':<18} detail")
    print("-" * 78)
    for c in checks:
        print(f"{c.name:<38} {c.outcome:<18} {c.detail}")
    print("=" * 78)
    print("\nRecord these numbers in DECISIONS.md under the OPEN item.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
