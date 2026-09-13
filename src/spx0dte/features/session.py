"""Session clock and volatility conventions shared by every Module B component.

Two conventions live here because getting them inconsistent between the
synthetic generator and the feature code silently biases the target:

- **Trading-minute annualisation.** Realised vol over `n` one-minute returns is
  `sqrt(MINUTES_PER_YEAR / n * sum(r^2))`, with a 252-day, 390-minute year.
- **VIX1D horizon scaling.** VIX1D quotes a calendar 24-hour vol that includes
  the overnight gap. Any 24-hour window taken from inside a session contains
  exactly one session (390 minutes) plus one overnight, so with an overnight
  share `w` of daily variance the per-minute session variance is
  `(1 - w) * (VIX1D/100)^2 / 365 / 390`. Re-annualised on the trading-minute
  convention that is `KAPPA * VIX1D / 100`, `KAPPA = sqrt((1 - w) * 252 / 365)`,
  independent of how many minutes remain.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

EASTERN = ZoneInfo("America/New_York")

SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
MINUTES_PER_SESSION = 390

TRADING_DAYS_PER_YEAR = 252
CALENDAR_DAYS_PER_YEAR = 365
MINUTES_PER_YEAR = TRADING_DAYS_PER_YEAR * MINUTES_PER_SESSION  # 98_280

# Share of a day's total variance that accrues overnight. A stylised fact for
# US indices is one quarter to one third; the exact value only shifts the
# target's mean, which the train-mean baseline absorbs.
OVERNIGHT_SHARE = 0.25

# SPEC.md section 2: vega collapses in the last half hour, so the target is
# undefined there. Fewer than five returns make the shortest RV window empty.
MIN_REMAINING_MINUTES = 30
MIN_ELAPSED_MINUTES = 5

# A session with more gaps than this is dropped rather than forward-filled.
MAX_MISSING_BARS = 10


def kappa(overnight_share: float = OVERNIGHT_SHARE) -> float:
    """Scale from a VIX1D level (in vol points / 100) to trading-minute annualised vol."""
    if not 0.0 <= overnight_share < 1.0:
        raise ValueError(f"overnight_share must be in [0, 1), got {overnight_share}")
    return math.sqrt((1.0 - overnight_share) * TRADING_DAYS_PER_YEAR / CALENDAR_DAYS_PER_YEAR)


def session_grid(day: date) -> pd.DatetimeIndex:
    """The 390 bar-start timestamps of one regular session, in UTC."""
    first = datetime.combine(day, SESSION_OPEN, tzinfo=EASTERN)
    grid = pd.date_range(first, periods=MINUTES_PER_SESSION, freq="min")
    return grid.tz_convert("UTC").as_unit("us")


def et_date(ts: pd.Series) -> pd.Series:
    """Eastern calendar date of each UTC timestamp, as `datetime.date` objects."""
    return ts.dt.tz_convert(EASTERN).dt.date


def minutes_to_close(t: pd.Series) -> pd.Series:
    """Whole minutes from decision time `t` (UTC) to 16:00 ET on the same Eastern date."""
    local = t.dt.tz_convert(EASTERN)
    close = local.dt.normalize() + timedelta(
        hours=SESSION_CLOSE.hour, minutes=SESSION_CLOSE.minute
    )
    return ((close - local).dt.total_seconds() // 60).astype("int64")
