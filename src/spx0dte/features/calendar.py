"""Macro event days for the `macro_event` feature.

FOMC decision days are published years ahead and listed explicitly. Non-farm
payrolls are approximated as the first Friday of each month, which is right
except when a holiday shifts the release. CPI is not included: its dates move
enough that a hard-coded list from memory is worse than no flag. Callers that
have a better calendar pass their own set to `build_features`.
"""

from __future__ import annotations

from datetime import date, timedelta

FOMC_DATES: frozenset[date] = frozenset({
    # 2024
    date(2024, 1, 31), date(2024, 3, 20), date(2024, 5, 1), date(2024, 6, 12),
    date(2024, 7, 31), date(2024, 9, 18), date(2024, 11, 7), date(2024, 12, 18),
    # 2025
    date(2025, 1, 29), date(2025, 3, 19), date(2025, 5, 7), date(2025, 6, 18),
    date(2025, 7, 30), date(2025, 9, 17), date(2025, 10, 29), date(2025, 12, 10),
    # 2026
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
    date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9),
})


def nfp_dates(year: int) -> frozenset[date]:
    """First Friday of every month in `year`."""
    out: set[date] = set()
    for month in range(1, 13):
        day = date(year, month, 1)
        day += timedelta(days=(4 - day.weekday()) % 7)
        out.add(day)
    return frozenset(out)


def macro_event_dates(years: tuple[int, ...] = (2024, 2025, 2026)) -> frozenset[date]:
    """FOMC decisions plus first-Friday payrolls for the given years."""
    out: set[date] = {d for d in FOMC_DATES if d.year in years}
    for year in years:
        out |= nfp_dates(year)
    return frozenset(out)
