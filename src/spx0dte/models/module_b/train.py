"""Train and evaluate the Module B variance-risk-premium model.

LightGBM regression on `log(RV_remaining / IV_current)`. Two things make the
number honest:

- **Split by date, not by row.** Adjacent minutes share almost all of their
  target; a row-wise split would test on the training set's twins. Training
  rows are also thinned to one every `stride` minutes for the same reason.
- **Two baselines, both must be beaten out-of-sample.** Zero (`IV = RV`, the
  claim that there is no premium) and the training-set mean (the claim that the
  premium is constant). A model that beats zero but not the mean has only
  learned the level, which a mis-set `kappa` would also produce.

Usage:
    .venv/bin/python -m spx0dte.models.module_b.train --synthetic --days 200
    .venv/bin/python -m spx0dte.models.module_b.train --bars data/bars --cboe data/cboe
    ... --record-run     # also insert a model_runs row in Supabase
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd

from spx0dte.features.calendar import macro_event_dates
from spx0dte.features.module_b import (
    CATEGORICAL_COLUMNS,
    DEFAULT_CONFIG,
    FEATURE_COLUMNS,
    TARGET,
    FeatureConfig,
    build_features,
)
from spx0dte.features.session import kappa

logger = logging.getLogger(__name__)

DEFAULT_ROOT = Path("data/models/module_b")

DEFAULT_PARAMS: dict[str, Any] = {
    "objective": "regression",
    "learning_rate": 0.03,
    "num_leaves": 15,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbose": -1,
}
NUM_BOOST_ROUND = 2000
EARLY_STOPPING_ROUNDS = 100


@dataclass(frozen=True)
class SplitSpec:
    """Date-ordered train / valid / test split, thinned to one row per `stride` minutes."""

    train_frac: float = 0.70
    valid_frac: float = 0.15
    stride: int = 5


DEFAULT_SPLIT = SplitSpec()


@dataclass(frozen=True)
class TrainResult:
    booster: lgb.Booster
    metrics: dict[str, Any]
    importance: dict[str, float]
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    params: dict[str, Any] = field(default_factory=dict)
    feature_config: FeatureConfig = DEFAULT_CONFIG
    split: SplitSpec = DEFAULT_SPLIT


def split_by_date(
    frame: pd.DataFrame, spec: SplitSpec = DEFAULT_SPLIT
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split on sorted unique dates. Every date lands in exactly one fold."""
    if not 0.0 < spec.train_frac < 1.0 or spec.train_frac + spec.valid_frac >= 1.0:
        raise ValueError(f"fractions must leave room for a test fold: {spec}")
    days = np.array(sorted(frame["date"].unique()))
    if len(days) < 3:
        raise ValueError(f"need at least 3 dates to split, got {len(days)}")
    n_train = max(1, int(len(days) * spec.train_frac))
    n_valid = max(1, int(len(days) * spec.valid_frac))
    if n_train + n_valid >= len(days):
        n_valid = len(days) - n_train - 1
    train_days = set(days[:n_train])
    valid_days = set(days[n_train : n_train + n_valid])
    minute = frame["minutes_to_close"] % spec.stride == 0
    thinned = frame[minute]
    in_train = thinned["date"].isin(train_days)
    in_valid = thinned["date"].isin(valid_days)
    return thinned[in_train], thinned[in_valid], thinned[~in_train & ~in_valid]


def evaluate(y: np.ndarray, pred: np.ndarray, train_mean: float) -> dict[str, float]:
    """MSE against the model and both baselines, plus R² and sign accuracy."""
    y, pred = np.asarray(y, dtype=float), np.asarray(pred, dtype=float)
    mse = float(np.mean((y - pred) ** 2))
    mse_zero = float(np.mean(y**2))
    mse_mean = float(np.mean((y - train_mean) ** 2))
    return {
        "n": int(len(y)),
        "mse": mse,
        "mse_zero": mse_zero,
        "mse_mean": mse_mean,
        "r2_vs_zero": 1.0 - mse / mse_zero if mse_zero > 0 else float("nan"),
        "r2_vs_mean": 1.0 - mse / mse_mean if mse_mean > 0 else float("nan"),
        "directional_accuracy": float(np.mean(np.sign(y) == np.sign(pred))),
    }


def _dataset(frame: pd.DataFrame, reference: lgb.Dataset | None = None) -> lgb.Dataset:
    return lgb.Dataset(
        frame[list(FEATURE_COLUMNS)],
        label=frame[TARGET],
        categorical_feature=list(CATEGORICAL_COLUMNS),
        reference=reference,
        free_raw_data=False,
    )


def train(
    frame: pd.DataFrame,
    *,
    split: SplitSpec = DEFAULT_SPLIT,
    params: dict[str, Any] | None = None,
    feature_config: FeatureConfig = DEFAULT_CONFIG,
    seed: int = 0,
) -> TrainResult:
    """Fit on the train fold with early stopping on valid, report on test."""
    params = {**DEFAULT_PARAMS, **(params or {}), "seed": seed}
    train_fold, valid_fold, test_fold = split_by_date(frame, split)
    logger.info(
        "split: train %d rows / %d days, valid %d / %d, test %d / %d",
        len(train_fold), train_fold["date"].nunique(),
        len(valid_fold), valid_fold["date"].nunique(),
        len(test_fold), test_fold["date"].nunique(),
    )
    train_set = _dataset(train_fold)
    valid_set = _dataset(valid_fold, reference=train_set)
    booster = lgb.train(
        params,
        train_set,
        num_boost_round=NUM_BOOST_ROUND,
        valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False)],
    )
    train_mean = float(train_fold[TARGET].mean())
    metrics = {
        "best_iteration": int(booster.best_iteration),
        "train_mean": train_mean,
        "n_train": int(len(train_fold)),
        "valid": evaluate(
            valid_fold[TARGET], booster.predict(valid_fold[list(FEATURE_COLUMNS)]), train_mean
        ),
        "test": evaluate(
            test_fold[TARGET], booster.predict(test_fold[list(FEATURE_COLUMNS)]), train_mean
        ),
    }
    gain = booster.feature_importance(importance_type="gain")
    total = float(gain.sum()) or 1.0
    importance = {
        name: float(g) / total
        for name, g in sorted(
            zip(booster.feature_name(), gain, strict=True), key=lambda x: -x[1]
        )
    }
    return TrainResult(
        booster=booster,
        metrics=metrics,
        importance=importance,
        train_start=train_fold["date"].min(),
        train_end=train_fold["date"].max(),
        test_start=test_fold["date"].min(),
        test_end=test_fold["date"].max(),
        params=params,
        feature_config=feature_config,
        split=split,
    )


def version_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def save_artifact(
    result: TrainResult, root: Path = DEFAULT_ROOT, version: str | None = None
) -> Path:
    """Write `model.txt`, `metrics.json` and `manifest.json` under `root/<version>/`."""
    version = version or version_stamp()
    directory = root / version
    directory.mkdir(parents=True, exist_ok=True)
    result.booster.save_model(directory / "model.txt", num_iteration=result.booster.best_iteration)
    (directory / "metrics.json").write_text(
        json.dumps({"metrics": result.metrics, "importance": result.importance}, indent=2)
    )
    manifest = {
        "module": "B",
        "version": version,
        "target": TARGET,
        "features": list(FEATURE_COLUMNS),
        "categorical": list(CATEGORICAL_COLUMNS),
        "feature_config": asdict(result.feature_config),
        "kappa": kappa(result.feature_config.overnight_share),
        "split": asdict(result.split),
        "train_start": result.train_start.isoformat(),
        "train_end": result.train_end.isoformat(),
        "test_start": result.test_start.isoformat(),
        "test_end": result.test_end.isoformat(),
        "params": result.params,
        "git_sha": _git_sha(),
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2))
    logger.info("saved artifact %s", directory)
    return directory


def _load_frame(args: argparse.Namespace) -> pd.DataFrame:
    if args.synthetic:
        from spx0dte.synth.spx import SynthConfig, generate

        data = generate(SynthConfig(n_days=args.days, seed=args.seed))
        return build_features(data.bars, event_dates=data.event_dates)

    from spx0dte.features.bars import load_bars

    bars = load_bars(args.bars)
    if args.cboe is not None:
        from spx0dte.ingest.cboe import daily_to_bars, load_daily

        present = set(bars["symbol"].unique())
        wanted = [s for s in ("VIX1D", "VIX9D", "VIX") if s not in present]
        if wanted:
            logger.info("filling %s from CBOE daily prints", wanted)
            bars = pd.concat([bars, daily_to_bars(load_daily(args.cboe, wanted))],
                             ignore_index=True)
    return build_features(bars, event_dates=macro_event_dates())


def main() -> int:
    parser = argparse.ArgumentParser(description="Train Module B (variance risk premium).")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--synthetic", action="store_true")
    source.add_argument("--bars", type=Path, help="root of real 1-min bars")
    parser.add_argument("--cboe", type=Path, help="CBOE daily parquet root for missing indices")
    parser.add_argument("--days", type=int, default=200, help="synthetic sessions")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--record-run", action="store_true", help="insert a model_runs row")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

    frame = _load_frame(args)
    logger.info("%d rows over %d sessions", len(frame), frame["date"].nunique())
    result = train(frame, seed=args.seed)
    test = result.metrics["test"]
    logger.info(
        "test: mse %.5f | zero %.5f | mean %.5f | r2_vs_zero %.3f | r2_vs_mean %.3f | dir %.3f",
        test["mse"], test["mse_zero"], test["mse_mean"],
        test["r2_vs_zero"], test["r2_vs_mean"], test["directional_accuracy"],
    )
    beats = test["mse"] < test["mse_zero"] and test["mse"] < test["mse_mean"]
    logger.info("beats both baselines out-of-sample: %s", beats)
    top = list(result.importance.items())[:5]
    logger.info("top features: %s", ", ".join(f"{k} {v:.2f}" for k, v in top))
    directory = save_artifact(result, args.out)

    if args.record_run:
        from spx0dte.db.client import model_run_row, record_model_run, supabase_client

        row = model_run_row(
            module="B",
            version=directory.name,
            train_start=result.train_start,
            train_end=result.train_end,
            metrics={
                **result.metrics,
                "importance": result.importance,
                "params": result.params,
                "source": "synthetic" if args.synthetic else str(args.bars),
            },
            artifact_path=str(directory),
        )
        run_id = record_model_run(supabase_client(), row)
        logger.info("model_runs row %d written", run_id)
    return 0 if beats else 2


if __name__ == "__main__":
    raise SystemExit(main())
