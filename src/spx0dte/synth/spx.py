"""Synthetic SPX sessions with a synthetic VIX1D that carries a *known* premium.

This is not a market model. It is a test fixture with the one property the
Module B pipeline needs and real data cannot give: the variance risk premium
is injected by construction, so a test can check that features, target and
model recover it, and that with the premium switched off the model finds
nothing (the leakage control).

Per session `d`:

- Daily vol `sigma_d = base_vol * exp(h_d)`, `h_d` an AR(1) around zero.
- Minute returns `r_k = sigma_d * s_k / sqrt(MPY) * z_k + J_k` with a U-shaped
  intraday profile `s_k` and rare jumps `J_k`. Every `event_every`-th session
  is an "event day" whose variance is boosted for half an hour after the open.
- The overnight gap has variance `sigma_d^2 / 252 * w / (1 - w)`, so the
  overnight share of daily variance is exactly `w = overnight_share` -- the
  same constant the feature code assumes, which makes `kappa` invert exactly.
- The generator knows `E[sum of remaining r^2]` at every minute, hence the
  fair remaining vol `RV_exp(k)`. The synthetic VIX1D is that fair vol times
  `exp(premium)`, times a slow AR(1) forecast noise, rescaled by `1/kappa` so
  the feature code's `IV_current = kappa * VIX1D / 100` lands back on it.
- `premium(m, sigma_d) = p0 + p1 * m/390 + p2 * log(sigma_d / base_vol)`: a
  function of minutes-to-close and vol level, both of which are features.

Everything is emitted in the shared bars schema under the same layout as the
IBKR backfill, so nothing downstream can tell synthetic from real.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from spx0dte.features.bars import COLUMNS, validate_bars
from spx0dte.features.session import (
    MINUTES_PER_SESSION,
    MINUTES_PER_YEAR,
    OVERNIGHT_SHARE,
    TRADING_DAYS_PER_YEAR,
    kappa,
    session_grid,
)

SYMBOLS = ("SPX", "VIX1D", "VIX9D", "VIX")


@dataclass(frozen=True)
class SynthConfig:
    """Knobs of the generator. Defaults produce a recoverable premium."""

    n_days: int = 200
    start: date = date(2026, 1, 5)
    seed: int = 0
    s0: float = 6500.0
    base_vol: float = 0.12  # annualised session vol, trading-minute convention
    vol_persistence: float = 0.9  # AR(1) coefficient on log daily vol
    vol_of_vol: float = 0.15  # innovation std of log daily vol
    seasonality_amp: float = 0.6  # U-shape depth; 0 = flat
    jump_prob_per_min: float = 1 / 2000
    jump_std: float = 0.004
    event_every: int = 10  # every n-th session is an event day; 0 = never
    event_boost: float = 3.0  # variance multiplier for minutes 30..60 on event days
    overnight_share: float = OVERNIGHT_SHARE
    premium_const: float = 0.03
    premium_minutes: float = 0.15
    premium_vol: float = 0.25
    forecast_noise: float = 0.03  # stationary std of the AR(1) log-noise on VIX1D
    forecast_noise_persistence: float = 0.95


NO_PREMIUM = SynthConfig(
    premium_const=0.0, premium_minutes=0.0, premium_vol=0.0, forecast_noise=0.0
)
WITH_PREMIUM = SynthConfig()


@dataclass(frozen=True)
class SynthData:
    """Bars in the shared schema plus the generator's private truth for tests."""

    bars: pd.DataFrame
    truth: pd.DataFrame  # per SPX bar: ts, date, k, sigma_day, rv_expected, premium, event
    event_dates: frozenset[date]


def trading_days(start: date, n_days: int) -> list[date]:
    """`n_days` consecutive weekdays from `start`. No holiday calendar: synthetic."""
    days: list[date] = []
    day = start
    while len(days) < n_days:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def intraday_profile(amp: float) -> np.ndarray:
    """Variance weights `w_k` with mean 1: a U-shape, deeper for larger `amp`."""
    k = np.arange(MINUTES_PER_SESSION)
    centre = (MINUTES_PER_SESSION - 1) / 2
    w = 1.0 + amp * ((k - centre) / centre) ** 2
    return w / w.mean()


def premium(m: np.ndarray, sigma_day: np.ndarray, config: SynthConfig) -> np.ndarray:
    """Log premium of implied over fair remaining vol, as a function of features."""
    return (
        config.premium_const
        + config.premium_minutes * (m / MINUTES_PER_SESSION)
        + config.premium_vol * np.log(sigma_day / config.base_vol)
    )


def _ar1(rng: np.random.Generator, n: int, phi: float, stationary_std: float) -> np.ndarray:
    """AR(1) path started from its stationary distribution."""
    if stationary_std == 0.0:
        return np.zeros(n)
    innovation = stationary_std * np.sqrt(1.0 - phi**2)
    out = np.empty(n)
    out[0] = rng.normal(0.0, stationary_std)
    for i in range(1, n):
        out[i] = phi * out[i - 1] + rng.normal(0.0, innovation)
    return out


def _ohlc(open_: np.ndarray, close: np.ndarray, wiggle: np.ndarray) -> tuple[np.ndarray, ...]:
    high = np.maximum(open_, close) * np.exp(np.abs(wiggle))
    low = np.minimum(open_, close) * np.exp(-np.abs(wiggle))
    return high, low


def generate(config: SynthConfig = WITH_PREMIUM) -> SynthData:
    """Simulate `config.n_days` sessions. Deterministic for a given `config.seed`."""
    rng = np.random.default_rng(config.seed)
    n, minutes = config.n_days, MINUTES_PER_SESSION
    days = trading_days(config.start, n)
    kap = kappa(config.overnight_share)

    # Daily vol level: AR(1) on log vol, innovation std = vol_of_vol.
    h = np.empty(n)
    h[0] = rng.normal(0.0, config.vol_of_vol)
    for d in range(1, n):
        h[d] = config.vol_persistence * h[d - 1] + rng.normal(0.0, config.vol_of_vol)
    sigma_day = config.base_vol * np.exp(h)

    # Variance weights per (day, minute): U-shape, boosted on event days.
    profile = intraday_profile(config.seasonality_amp)
    w = np.tile(profile, (n, 1))
    is_event = np.zeros(n, dtype=bool)
    if config.event_every > 0:
        is_event[config.event_every - 1 :: config.event_every] = True
        w[is_event, 30:60] *= config.event_boost

    per_min_var = (sigma_day[:, None] ** 2) * w / MINUTES_PER_YEAR  # (n, 390)
    jump_var = config.jump_prob_per_min * config.jump_std**2

    # Minute returns.
    z = rng.standard_normal((n, minutes))
    jumps = rng.random((n, minutes)) < config.jump_prob_per_min
    jump_size = rng.normal(0.0, config.jump_std, (n, minutes))
    r = np.sqrt(per_min_var) * z + np.where(jumps, jump_size, 0.0)

    # Overnight gaps: share `w` of total daily variance.
    session_var = sigma_day**2 / TRADING_DAYS_PER_YEAR
    gap_var = session_var * config.overnight_share / (1.0 - config.overnight_share)
    gap = rng.normal(0.0, np.sqrt(gap_var))

    # Prices.
    close = np.empty((n, minutes))
    open_ = np.empty((n, minutes))
    prev_close = config.s0
    for d in range(n):
        open_[d, 0] = prev_close * np.exp(gap[d])
        close[d] = open_[d, 0] * np.exp(np.cumsum(r[d]))
        open_[d, 1:] = close[d, :-1]
        prev_close = close[d, -1]
    wiggle = rng.normal(0.0, 0.2 * np.sqrt(per_min_var))
    high, low = _ohlc(open_, close, wiggle)
    volume = np.exp(rng.normal(np.log(2e5), 0.4, (n, minutes)))

    # Fair remaining vol after bar k, from the generator's own variance.
    expected = per_min_var + jump_var
    remaining_var = expected[:, ::-1].cumsum(axis=1)[:, ::-1] - expected  # sum over j > k
    m = (minutes - 1 - np.arange(minutes))[None, :].repeat(n, axis=0).astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        rv_expected = np.sqrt(MINUTES_PER_YEAR / m * remaining_var)
    rv_expected[:, -1] = rv_expected[:, -2]  # m = 0: undefined, never used
    prem = premium(m, sigma_day[:, None].repeat(minutes, axis=1), config)
    noise = _ar1(rng, n * minutes, config.forecast_noise_persistence, config.forecast_noise)
    vix1d = 100.0 * rv_expected * np.exp(prem) / kap * np.exp(noise.reshape(n, minutes))

    # Longer tenors: level references without a premium structure.
    vix9d = 100.0 * sigma_day[:, None] / kap * 1.05 * np.exp(rng.normal(0, 0.01, (n, minutes)))
    vix = 100.0 * config.base_vol / kap * 1.10 * np.exp(rng.normal(0, 0.002, (n, minutes)))

    ts = pd.DatetimeIndex(np.concatenate([session_grid(day).values for day in days])).tz_localize(
        "UTC"
    )

    def frame(symbol: str, o: np.ndarray, hi: np.ndarray, lo: np.ndarray, c: np.ndarray,
              vol: np.ndarray) -> pd.DataFrame:
        return pd.DataFrame({
            "ts": ts,
            "symbol": symbol,
            "open": o.ravel(),
            "high": hi.ravel(),
            "low": lo.ravel(),
            "close": c.ravel(),
            "volume": vol.ravel(),
        })

    zero_volume = np.zeros((n, minutes))
    frames = [frame("SPX", open_, high, low, close, volume)]
    for symbol, level in (("VIX1D", vix1d), ("VIX9D", vix9d), ("VIX", vix)):
        level_open = np.concatenate([level[:, :1], level[:, :-1]], axis=1)
        hi, lo = np.maximum(level_open, level), np.minimum(level_open, level)
        frames.append(frame(symbol, level_open, hi, lo, level, zero_volume))
    bars = validate_bars(pd.concat(frames, ignore_index=True)[list(COLUMNS)])

    truth = pd.DataFrame({
        "ts": ts,
        "date": np.repeat(days, minutes),
        "k": np.tile(np.arange(minutes), n),
        "sigma_day": np.repeat(sigma_day, minutes),
        "rv_expected": rv_expected.ravel(),
        "premium": prem.ravel(),
        "event": np.repeat(is_event, minutes),
    })
    event_dates = frozenset(day for day, flag in zip(days, is_event, strict=True) if flag)
    return SynthData(bars=bars, truth=truth, event_dates=event_dates)
