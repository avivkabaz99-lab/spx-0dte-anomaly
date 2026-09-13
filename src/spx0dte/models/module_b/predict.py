"""Load a saved Module B artifact and predict from a feature frame."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lightgbm as lgb
import pandas as pd

from spx0dte.models.module_b.train import DEFAULT_ROOT


@dataclass(frozen=True)
class Predictor:
    booster: lgb.Booster
    manifest: dict[str, Any]

    @property
    def features(self) -> list[str]:
        return list(self.manifest["features"])

    def predict(self, features: pd.DataFrame) -> pd.Series:
        """Predicted `log(RV_remaining / IV_current)`; negative means implied is rich."""
        missing = [c for c in self.features if c not in features.columns]
        if missing:
            raise ValueError(f"feature frame is missing {missing}")
        pred = self.booster.predict(features[self.features])
        return pd.Series(pred, index=features.index, name="pred")


def latest_version(root: Path = DEFAULT_ROOT) -> Path:
    """Most recent artifact directory under `root` (versions are UTC timestamps)."""
    candidates = sorted(p for p in root.glob("*") if (p / "model.txt").exists())
    if not candidates:
        raise FileNotFoundError(f"no Module B artifact under {root}")
    return candidates[-1]


def load_model(version_dir: Path) -> Predictor:
    manifest = json.loads((version_dir / "manifest.json").read_text())
    booster = lgb.Booster(model_file=str(version_dir / "model.txt"))
    return Predictor(booster=booster, manifest=manifest)
