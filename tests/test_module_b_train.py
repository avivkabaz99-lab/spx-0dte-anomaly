"""Module B training: date split, premium recovery, leakage control, artifact round trip."""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("lightgbm")

from spx0dte.features.module_b import FEATURE_COLUMNS, TARGET, build_features  # noqa: E402
from spx0dte.models.module_b.predict import latest_version, load_model  # noqa: E402
from spx0dte.models.module_b.train import (  # noqa: E402
    SplitSpec,
    evaluate,
    save_artifact,
    split_by_date,
    train,
)
from spx0dte.synth.spx import NO_PREMIUM, WITH_PREMIUM, generate  # noqa: E402


@pytest.fixture(scope="module")
def with_premium() -> pd.DataFrame:
    data = generate(dataclasses.replace(WITH_PREMIUM, n_days=200, seed=0))
    return build_features(data.bars, event_dates=data.event_dates)


@pytest.fixture(scope="module")
def without_premium() -> pd.DataFrame:
    data = generate(dataclasses.replace(NO_PREMIUM, n_days=200, seed=0))
    return build_features(data.bars, event_dates=data.event_dates)


def test_split_by_date_is_disjoint_ordered_and_thinned(with_premium: pd.DataFrame) -> None:
    train_fold, valid_fold, test_fold = split_by_date(with_premium, SplitSpec(stride=5))
    assert train_fold["date"].max() < valid_fold["date"].min() < test_fold["date"].min()
    assert not set(train_fold["date"]) & set(test_fold["date"])
    for fold in (train_fold, valid_fold, test_fold):
        assert (fold["minutes_to_close"] % 5 == 0).all()
    total_days = with_premium["date"].nunique()
    assert train_fold["date"].nunique() == int(total_days * 0.7)
    expected_test = total_days - int(total_days * 0.7) - int(total_days * 0.15)
    assert test_fold["date"].nunique() == expected_test


def test_split_rejects_impossible_fractions(with_premium: pd.DataFrame) -> None:
    with pytest.raises(ValueError, match="fractions"):
        split_by_date(with_premium, SplitSpec(train_frac=0.9, valid_frac=0.2))


def test_evaluate_baselines() -> None:
    y = np.array([1.0, -1.0, 2.0, -2.0])
    out = evaluate(y, np.zeros(4), train_mean=0.0)
    assert out["mse"] == out["mse_zero"] == out["mse_mean"] == 2.5
    assert out["r2_vs_zero"] == 0.0
    perfect = evaluate(y, y, train_mean=0.5)
    assert perfect["mse"] == 0.0 and perfect["directional_accuracy"] == 1.0


def test_model_recovers_injected_premium(with_premium: pd.DataFrame) -> None:
    result = train(with_premium, seed=0)
    test = result.metrics["test"]
    assert test["mse"] < 0.80 * test["mse_zero"]
    assert test["mse"] < 0.90 * test["mse_mean"]
    assert test["r2_vs_mean"] > 0.15
    top4 = list(result.importance)[:4]
    assert "minutes_to_close" in top4
    assert {"vix1d", "ts_1d_30", "ts_1d_9d"} & set(top4)
    assert abs(sum(result.importance.values()) - 1.0) < 1e-9


def test_no_premium_means_nothing_beyond_a_constant(without_premium: pd.DataFrame) -> None:
    """The leakage guard: with the premium off, the model must not beat the train mean.

    It may still beat zero slightly: rare jumps make `log(RV)` concave-biased
    (Jensen), a constant offset the mean baseline exists to absorb.
    """
    result = train(without_premium, seed=0)
    assert result.metrics["test"]["r2_vs_mean"] < 0.05


def test_artifact_round_trip(with_premium: pd.DataFrame, tmp_path) -> None:
    result = train(with_premium, seed=0)
    directory = save_artifact(result, tmp_path, version="v-test")
    assert {p.name for p in directory.iterdir()} == {"model.txt", "metrics.json", "manifest.json"}
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["features"] == list(FEATURE_COLUMNS)
    assert manifest["module"] == "B" and manifest["target"] == TARGET
    metrics = json.loads((directory / "metrics.json").read_text())
    assert metrics["metrics"]["test"]["mse"] == result.metrics["test"]["mse"]

    predictor = load_model(latest_version(tmp_path))
    sample = with_premium.tail(50)
    np.testing.assert_allclose(
        predictor.predict(sample).to_numpy(),
        result.booster.predict(sample[list(FEATURE_COLUMNS)],
                               num_iteration=result.booster.best_iteration),
        atol=1e-9,
    )
    with pytest.raises(ValueError, match="missing"):
        predictor.predict(sample.drop(columns=["vix1d"]))


def test_latest_version_without_artifacts(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        latest_version(tmp_path)
