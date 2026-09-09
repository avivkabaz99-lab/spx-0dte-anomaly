"""Contract builders for the instruments this project reads.

Kept out of the scripts so the spikes and the recorder resolve the same
contracts; a discrepancy here would silently compare different instruments.
"""

from __future__ import annotations

from ibapi.contract import Contract


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

    Args:
        expiry: `YYYYMMDD`.
        strike: Strike, or None to match every strike in the expiry.
        right: "C" or "P".
        include_expired: Required to resolve an expiry that has already passed.
            Only the previous trading day is retrievable (see DECISIONS.md).
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
