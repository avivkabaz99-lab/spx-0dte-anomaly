"""The synthetic generator: schema, determinism, and that its truth is self-consistent."""

from __future__ import annotations

import numpy as np
import pandas as pd

from spx0dte.features.bars import validate_bars
from spx0dte.features.session import MINUTES_PER_SESSION
from spx0dte.synth.spx import (
    NO_PREMIUM,
    SynthConfig,
    generate,
    intraday_profile,
    premium,
    trading_days,
)

SMALL = SynthConfig(n_days=12, seed=7)


def test_bars_conform_and_cover_every_session_minute() -> None:
    data = generate(SMALL)
    validate_bars(data.bars)  # raises on any defect
    counts = data.bars.groupby("symbol").size()
    symbols = ("SPX", "VIX", "VIX1D", "VIX9D")
    assert counts.to_dict() == {s: 12 * MINUTES_PER_SESSION for s in symbols}
    assert (data.bars["close"] > 0).all()
    assert (data.bars["high"] >= data.bars[["open", "close"]].max(axis=1)).all()
    assert (data.bars["low"] <= data.bars[["open", "close"]].min(axis=1)).all()


def test_deterministic_under_seed() -> None:
    a, b = generate(SMALL), generate(SMALL)
    pd.testing.assert_frame_equal(a.bars, b.bars)
    assert not generate(SynthConfig(n_days=12, seed=8)).bars.equals(a.bars)


def test_trading_days_skip_weekends() -> None:
    days = trading_days(SMALL.start, 12)
    assert all(d.weekday() < 5 for d in days)
    assert len(days) == 12


def test_event_days_are_every_nth_session() -> None:
    data = generate(SynthConfig(n_days=25, event_every=10))
    days = trading_days(data.truth["date"].iloc[0], 25)
    assert data.event_dates == {days[9], days[19]}


def test_truth_premium_matches_formula() -> None:
    data = generate(SMALL)
    t = data.truth
    m = (MINUTES_PER_SESSION - 1 - t["k"]).to_numpy(dtype=float)
    expected = premium(m, t["sigma_day"].to_numpy(), SMALL)
    np.testing.assert_allclose(t["premium"].to_numpy(), expected)


def test_no_premium_vix1d_is_fair_vol_exactly() -> None:
    """With premium and noise off, VIX1D * kappa / 100 is the generator's fair remaining vol."""
    from spx0dte.features.session import kappa

    data = generate(SynthConfig(n_days=6, **{k: getattr(NO_PREMIUM, k) for k in
                    ("premium_const", "premium_minutes", "premium_vol", "forecast_noise")}))
    vix1d = data.bars.loc[data.bars["symbol"] == "VIX1D", "close"].to_numpy()
    np.testing.assert_allclose(vix1d * kappa() / 100.0, data.truth["rv_expected"].to_numpy())


def test_overnight_gap_present() -> None:
    spx = generate(SMALL).bars.query("symbol == 'SPX'").reset_index(drop=True)
    opens = spx["open"].to_numpy()[::MINUTES_PER_SESSION][1:]
    prev_closes = spx["close"].to_numpy()[MINUTES_PER_SESSION - 1 :: MINUTES_PER_SESSION][:-1]
    assert not np.allclose(opens, prev_closes)


def test_intraday_profile_has_unit_mean_and_is_u_shaped() -> None:
    w = intraday_profile(0.6)
    assert abs(w.mean() - 1.0) < 1e-12
    assert w[0] > w[195] < w[-1]
    assert np.allclose(intraday_profile(0.0), 1.0)
