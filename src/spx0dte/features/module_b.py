"""Module B features and target: realised vol remaining against implied.

Every row is one decision time `t` inside one session: the end of bar `k`,
`t = ts_k + 1 min`. Features use bars `0..k`, the target uses bars `k+1..389`,
and nothing else. The tests enforce that split by perturbing the future and
checking features, then perturbing the past and checking the target.

    target = log(RV_remaining) - log(IV_current)
    RV_remaining = sqrt(MPY / m * sum_{i>k} r_i^2),   m = 389 - k
    IV_current   = kappa * VIX1D_t / 100              (see features/session.py)

A negative prediction says implied is rich: short premium. Rows in the last
`min_remaining` minutes are dropped after everything is computed, because the
target there is a handful of returns and SPEC.md calls that regime unusable.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from spx0dte.features.session import (
    MAX_MISSING_BARS,
    MIN_ELAPSED_MINUTES,
    MIN_REMAINING_MINUTES,
    MINUTES_PER_SESSION,
    MINUTES_PER_YEAR,
    OVERNIGHT_SHARE,
    et_date,
    kappa,
    session_grid,
)

logger = logging.getLogger(__name__)

RV_WINDOWS = (5, 15, 30, 60)

FEATURE_COLUMNS: tuple[str, ...] = (
    "rv_5", "rv_15", "rv_30", "rv_60",
    "overnight_gap", "minutes_to_close",
    "vix1d", "vix1d_chg_30", "vix1d_chg_open", "ts_1d_9d", "ts_1d_30",
    "ret_open", "ret_30", "trend_60",
    "dow", "macro_event", "prior_day_spread",
)
CATEGORICAL_COLUMNS: tuple[str, ...] = ("dow",)
TARGET = "target"
META_COLUMNS: tuple[str, ...] = ("ts", "date", "rv_remaining", "iv_current")

ONE_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class FeatureConfig:
    """Row filters and the one vol convention the target depends on."""

    overnight_share: float = OVERNIGHT_SHARE
    min_remaining: int = MIN_REMAINING_MINUTES
    min_elapsed: int = MIN_ELAPSED_MINUTES
    max_missing_bars: int = MAX_MISSING_BARS


DEFAULT_CONFIG = FeatureConfig()


def realized_vol(r2_sum: pd.Series, n_minutes: pd.Series | int) -> pd.Series:
    """Annualised vol from a sum of squared one-minute log returns over `n_minutes`."""
    return np.sqrt(MINUTES_PER_YEAR / n_minutes * r2_sum)


def session_returns(spx: pd.DataFrame, config: FeatureConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    """Put SPX bars on the 390-minute grid per session and compute minute returns.

    Sessions missing more than `max_missing_bars`, or missing the 15:59 bar
    (an early close), are dropped. Gaps inside a kept session are forward-
    filled, which is a zero return -- an honest "nothing printed".

    Returns columns: ts, t, date, k, open, close, r. Sorted by ts.
    """
    if spx.empty:
        raise ValueError("no SPX bars")
    spx = spx.sort_values("ts").reset_index(drop=True)
    days = et_date(spx["ts"])
    kept: list[pd.DataFrame] = []
    for day, group in spx.groupby(days, sort=True):
        grid = session_grid(day)
        on_grid = group.set_index("ts").reindex(grid)
        missing = int(on_grid["close"].isna().sum())
        if missing > config.max_missing_bars:
            logger.info("dropping %s: %d bars missing", day, missing)
            continue
        if np.isnan(on_grid["close"].iloc[-1]):
            logger.info("dropping %s: no 15:59 bar (early close)", day)
            continue
        close = on_grid["close"].ffill().bfill().to_numpy()
        open_ = float(group["open"].iloc[0])
        r = np.diff(np.log(close), prepend=np.log(open_))
        kept.append(pd.DataFrame({
            "ts": grid,
            "t": grid + ONE_MINUTE,
            "date": day,
            "k": np.arange(MINUTES_PER_SESSION),
            "open": open_,
            "close": close,
            "r": r,
        }))
    if not kept:
        raise ValueError("every session was dropped")
    return pd.concat(kept, ignore_index=True)


def vol_levels(bars: pd.DataFrame) -> pd.DataFrame:
    """Wide frame of index closes keyed by when they became known (bar end).

    Columns: known_at, vix1d, vix9d, vix. Symbols absent from `bars` become
    all-NaN columns so the feature set is stable across data sources.
    """
    levels = bars[bars["symbol"].isin(["VIX1D", "VIX9D", "VIX"])]
    wide = levels.pivot(index="ts", columns="symbol", values="close")
    wide = wide.rename(columns={"VIX1D": "vix1d", "VIX9D": "vix9d", "VIX": "vix"})
    for column in ("vix1d", "vix9d", "vix"):
        if column not in wide.columns:
            wide[column] = np.nan
    wide = wide[["vix1d", "vix9d", "vix"]].reset_index()
    wide["known_at"] = wide["ts"] + ONE_MINUTE
    return wide.drop(columns="ts").sort_values("known_at").reset_index(drop=True)


def _rolling_sum(frame: pd.DataFrame, column: str, window: int) -> pd.Series:
    return frame.groupby("date")[column].transform(
        lambda s: s.rolling(window, min_periods=window).sum()
    )


def build_features(
    bars: pd.DataFrame,
    *,
    event_dates: Iterable[date] = frozenset(),
    config: FeatureConfig = DEFAULT_CONFIG,
) -> pd.DataFrame:
    """One row per (session, minute) with META_COLUMNS + FEATURE_COLUMNS + TARGET.

    Features may be NaN (LightGBM handles it); the target never is -- rows
    without a finite target are dropped and counted in the log.
    """
    events = frozenset(event_dates)
    kap = kappa(config.overnight_share)
    rows = session_returns(bars[bars["symbol"] == "SPX"], config)
    rows["r2"] = rows["r"] ** 2

    # -- past-only features ------------------------------------------------
    for window in RV_WINDOWS:
        rows[f"rv_{window}"] = realized_vol(_rolling_sum(rows, "r2", window), window)

    day_first = rows.groupby("date")
    prev_close = rows.groupby("date")["close"].last().shift(1)
    rows["overnight_gap"] = np.log(rows["open"] / rows["date"].map(prev_close))
    rows["minutes_to_close"] = MINUTES_PER_SESSION - 1 - rows["k"]

    levels = vol_levels(bars)
    rows = pd.merge_asof(rows, levels, left_on="t", right_on="known_at", direction="backward")
    rows = rows.drop(columns="known_at")
    rows["vix1d_chg_30"] = rows["vix1d"] - rows.groupby("date")["vix1d"].shift(30)
    rows["vix1d_chg_open"] = rows["vix1d"] - rows.groupby("date")["vix1d"].transform("first")
    rows["ts_1d_9d"] = rows["vix1d"] / rows["vix9d"]
    rows["ts_1d_30"] = rows["vix1d"] / rows["vix"]

    rows["ret_open"] = np.log(rows["close"] / rows["open"])
    rows["ret_30"] = np.log(rows["close"] / rows.groupby("date")["close"].shift(30))
    sma_60 = rows.groupby("date")["close"].transform(
        lambda s: s.rolling(60, min_periods=60).mean()
    )
    rows["trend_60"] = rows["close"] / sma_60 - 1.0

    rows["dow"] = rows["date"].map(lambda d: d.weekday()).astype("int64")
    rows["macro_event"] = rows["date"].map(lambda d: int(d in events)).astype("int64")

    # Previous session: its full-day realised vol against what VIX1D said at
    # 09:35 that morning (row k = 4, the first row that survives min_elapsed).
    rv_full = realized_vol(day_first["r2"].sum(), MINUTES_PER_SESSION)
    iv_0935 = rows[rows["k"] == 4].set_index("date")["vix1d"] * kap / 100.0
    spread = np.log(rv_full / iv_0935).shift(1)
    rows["prior_day_spread"] = rows["date"].map(spread)

    # -- future-only target ------------------------------------------------
    remaining = rows.groupby("date")["r2"].transform(lambda s: s[::-1].cumsum()[::-1]) - rows["r2"]
    with np.errstate(divide="ignore", invalid="ignore"):
        rows["rv_remaining"] = realized_vol(remaining, rows["minutes_to_close"])
        rows["iv_current"] = kap * rows["vix1d"] / 100.0
        rows[TARGET] = np.log(rows["rv_remaining"]) - np.log(rows["iv_current"])

    # -- filters, applied last so nothing above depends on them ---------------
    in_window = (rows["minutes_to_close"] >= config.min_remaining) & (
        rows["k"] + 1 >= config.min_elapsed
    )
    finite = np.isfinite(rows[TARGET])
    dropped = int((in_window & ~finite).sum())
    if dropped:
        logger.warning("dropped %d rows with no finite target (missing VIX1D?)", dropped)
    out = rows[in_window & finite]
    columns = [*META_COLUMNS, *FEATURE_COLUMNS, TARGET]
    return out[columns].sort_values("ts").reset_index(drop=True)
