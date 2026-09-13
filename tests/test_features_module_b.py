"""Module B features: exact values on a constructed day, and the past/future split.

The two perturbation tests are the ones that matter. Leakage in a vol model
looks like skill, so the split between "features see only the past" and
"target sees only the future" is asserted, not assumed.
"""

from __future__ import annotations

import dataclasses
from datetime import date

import numpy as np
import pandas as pd
import pytest

from spx0dte.features.calendar import FOMC_DATES, macro_event_dates, nfp_dates
from spx0dte.features.module_b import (
    FEATURE_COLUMNS,
    META_COLUMNS,
    TARGET,
    FeatureConfig,
    build_features,
    session_returns,
)
from spx0dte.features.session import MINUTES_PER_SESSION, MINUTES_PER_YEAR, kappa, session_grid
from spx0dte.synth.spx import NO_PREMIUM, SynthConfig, generate

DAY = date(2026, 9, 9)  # a Wednesday
NEXT = date(2026, 9, 10)


def constant_return_day(day: date, r: float, level: float = 100.0, vix1d: float = 20.0,
                        drop: list[int] | None = None) -> pd.DataFrame:
    """SPX opening at `level` with every minute returning `r`, plus flat VIX1D / VIX9D / VIX."""
    grid = session_grid(day)
    k = np.arange(MINUTES_PER_SESSION)
    close = level * np.exp((k + 1) * r)
    open_ = np.concatenate([[level], close[:-1]])
    spx = pd.DataFrame({"ts": grid, "symbol": "SPX", "open": open_, "high": close + 1,
                        "low": open_ - 1, "close": close, "volume": 1.0})
    if drop:
        spx = spx.drop(index=drop)
    frames = [spx]
    for symbol, value in (("VIX1D", vix1d), ("VIX9D", vix1d * 0.9), ("VIX", vix1d * 0.8)):
        frames.append(pd.DataFrame({"ts": grid, "symbol": symbol, "open": value, "high": value,
                                    "low": value, "close": value, "volume": 0.0}))
    return pd.concat(frames, ignore_index=True)


def test_columns_and_row_window() -> None:
    out = build_features(constant_return_day(DAY, 1e-4))
    assert list(out.columns) == [*META_COLUMNS, *FEATURE_COLUMNS, TARGET]
    assert out["minutes_to_close"].iloc[0] == MINUTES_PER_SESSION - 5  # k = 4 survives
    assert out["minutes_to_close"].iloc[-1] == 30
    assert len(out) == MINUTES_PER_SESSION - 30 - 4
    assert out[TARGET].notna().all()


def test_exact_values_on_a_constant_return_day() -> None:
    r = 2e-4
    out = build_features(constant_return_day(DAY, r, vix1d=20.0))
    expected_rv = np.sqrt(r**2 * MINUTES_PER_YEAR)
    np.testing.assert_allclose(out["rv_remaining"], expected_rv, rtol=1e-9)
    np.testing.assert_allclose(out["rv_5"], expected_rv, rtol=1e-9)
    np.testing.assert_allclose(out["rv_60"].dropna(), expected_rv, rtol=1e-9)
    np.testing.assert_allclose(out["iv_current"], kappa() * 20.0 / 100.0)
    np.testing.assert_allclose(out[TARGET], np.log(expected_rv) - np.log(kappa() * 0.2))
    assert (out["dow"] == 2).all()
    assert (out["macro_event"] == 0).all()
    assert out["overnight_gap"].isna().all()  # first day has no previous close
    assert out["prior_day_spread"].isna().all()
    np.testing.assert_allclose(out["ts_1d_9d"], 1 / 0.9)
    np.testing.assert_allclose(out["ts_1d_30"], 1 / 0.8)
    k = MINUTES_PER_SESSION - 1 - out["minutes_to_close"]
    np.testing.assert_allclose(out["ret_open"], (k + 1) * r, atol=1e-12)


def test_second_day_sees_previous_session() -> None:
    day1 = constant_return_day(DAY, 1e-4, level=100.0)
    day2 = constant_return_day(NEXT, 3e-4, level=110.0, vix1d=25.0)
    out = build_features(pd.concat([day1, day2], ignore_index=True))
    second = out[out["date"] == NEXT]
    prev_close = 100.0 * np.exp(390 * 1e-4)
    np.testing.assert_allclose(second["overnight_gap"], np.log(110.0 / prev_close))
    rv_full_prev = np.sqrt(1e-4**2 * MINUTES_PER_YEAR)
    np.testing.assert_allclose(second["prior_day_spread"],
                               np.log(rv_full_prev / (kappa() * 20.0 / 100.0)))
    assert (second["dow"] == 3).all()


def test_features_are_blind_to_the_future_and_target_to_the_past() -> None:
    data = generate(SynthConfig(n_days=3, seed=1))
    base = build_features(data.bars, event_dates=data.event_dates)
    pivot = base["ts"].iloc[len(base) // 2]

    future = data.bars.copy()
    later = future["ts"] > pivot  # bars starting after the pivot bar
    future.loc[later, ["open", "high", "low", "close"]] *= 1.05
    out = build_features(future, event_dates=data.event_dates)
    at_pivot = out[out["ts"] == pivot].iloc[0]
    pd.testing.assert_series_equal(at_pivot[list(FEATURE_COLUMNS)],
                                   base[base["ts"] == pivot].iloc[0][list(FEATURE_COLUMNS)])
    assert at_pivot[TARGET] != base[base["ts"] == pivot].iloc[0][TARGET]

    past = data.bars.copy()
    same_day = past["ts"].dt.date == pivot.date()
    earlier = same_day & (past["ts"] < pivot) & (past["symbol"] == "SPX")
    past.loc[earlier, "close"] *= 1.01
    out = build_features(past, event_dates=data.event_dates)
    at_pivot = out[out["ts"] == pivot].iloc[0]
    base_pivot = base[base["ts"] == pivot].iloc[0]
    assert np.isclose(at_pivot["rv_remaining"], base_pivot["rv_remaining"])
    assert at_pivot["rv_5"] != base_pivot["rv_5"]


def test_vix1d_print_is_known_one_minute_after_its_bar_starts() -> None:
    bars = constant_return_day(DAY, 1e-4, vix1d=20.0)
    grid = session_grid(DAY)
    spike_at = grid[100]
    bars.loc[(bars["symbol"] == "VIX1D") & (bars["ts"] == spike_at), "close"] = 30.0
    out = build_features(bars).set_index("ts")
    assert out.loc[grid[99], "vix1d"] == 20.0
    assert out.loc[grid[100], "vix1d"] == 30.0  # decision time = end of bar 100
    assert out.loc[grid[101], "vix1d"] == 20.0


def test_event_flag_from_calendar() -> None:
    fomc = date(2026, 9, 16)
    out = build_features(constant_return_day(fomc, 1e-4), event_dates=macro_event_dates())
    assert (out["macro_event"] == 1).all()
    assert fomc in FOMC_DATES
    assert date(2026, 9, 4) in nfp_dates(2026)


def test_gappy_and_early_close_sessions() -> None:
    few_missing = constant_return_day(DAY, 1e-4, drop=list(range(50, 58)))
    kept = session_returns(few_missing[few_missing["symbol"] == "SPX"])
    assert len(kept) == MINUTES_PER_SESSION
    assert (kept.loc[50:57, "r"] == 0).all()  # forward-filled
    assert kept.loc[58, "r"] == pytest.approx(9 * 1e-4)  # the catch-up return

    many_missing = constant_return_day(DAY, 1e-4, drop=list(range(50, 62)))
    with pytest.raises(ValueError, match="every session was dropped"):
        session_returns(many_missing[many_missing["symbol"] == "SPX"])

    early_close = constant_return_day(DAY, 1e-4, drop=list(range(385, 390)))
    with pytest.raises(ValueError, match="every session was dropped"):
        session_returns(early_close[early_close["symbol"] == "SPX"])


def test_missing_vix_symbols_become_nan_columns_and_rows_are_dropped() -> None:
    bars = constant_return_day(DAY, 1e-4)
    only_spx = bars[bars["symbol"] == "SPX"]
    out = build_features(only_spx)
    assert out.empty  # no VIX1D -> no target anywhere


def test_calibration_without_premium_or_jumps() -> None:
    """kappa must invert the generator's overnight share: the target is centred."""
    config = dataclasses.replace(NO_PREMIUM, n_days=200, jump_prob_per_min=0.0)
    out = build_features(generate(config).bars)
    assert abs(out[TARGET].mean()) < 0.01


def test_realized_vol_level_matches_generator() -> None:
    config = SynthConfig(n_days=150, base_vol=0.12, seasonality_amp=0.0, vol_of_vol=0.0,
                         jump_prob_per_min=0.0, event_every=0)
    out = build_features(generate(config).bars)
    assert abs(out["rv_remaining"].mean() / 0.12 - 1.0) < 0.03


def test_feature_config_filters() -> None:
    out = build_features(constant_return_day(DAY, 1e-4),
                         config=FeatureConfig(min_remaining=60, min_elapsed=10))
    assert out["minutes_to_close"].min() == 60
    assert out["minutes_to_close"].max() == MINUTES_PER_SESSION - 10
