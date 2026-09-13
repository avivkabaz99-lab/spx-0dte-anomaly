"""The model_runs row builder and the thin insert wrapper, against a fake client."""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pytest

from spx0dte.db.client import jsonable, list_model_runs, model_run_row, record_model_run


class FakeQuery:
    def __init__(self, sink: dict[str, Any], data: list[dict[str, Any]]) -> None:
        self.sink, self.data = sink, data

    def insert(self, row: dict[str, Any]) -> FakeQuery:
        self.sink["inserted"] = row
        return self

    def select(self, columns: str) -> FakeQuery:
        self.sink["select"] = columns
        return self

    def eq(self, column: str, value: Any) -> FakeQuery:
        self.sink["eq"] = (column, value)
        return self

    def order(self, column: str, desc: bool = False) -> FakeQuery:
        self.sink["order"] = (column, desc)
        return self

    def limit(self, n: int) -> FakeQuery:
        self.sink["limit"] = n
        return self

    def execute(self) -> Any:
        return type("Response", (), {"data": self.data})()


class FakeClient:
    def __init__(self, data: list[dict[str, Any]] | None = None) -> None:
        self.sink: dict[str, Any] = {}
        self.data = data or [{"id": 42}]

    def table(self, name: str) -> FakeQuery:
        self.sink["table"] = name
        return FakeQuery(self.sink, self.data)


def test_jsonable_flattens_numpy_dates_and_nan() -> None:
    out = jsonable({
        "f32": np.float32(1.5),
        "i64": np.int64(3),
        "nan": float("nan"),
        "inf": np.inf,
        "day": date(2026, 9, 9),
        "nested": [np.float64(0.25), {"x": np.nan}],
    })
    assert out == {
        "f32": 1.5, "i64": 3, "nan": None, "inf": None, "day": "2026-09-09",
        "nested": [0.25, {"x": None}],
    }
    assert type(out["f32"]) is float and type(out["i64"]) is int


def test_model_run_row_is_serialisable() -> None:
    row = model_run_row(
        module="B", version="v1", train_start=date(2026, 1, 5), train_end=date(2026, 6, 30),
        metrics={"test": {"mse": np.float64(0.004), "r2": np.nan}}, artifact_path="data/x",
    )
    assert row == {
        "module": "B", "version": "v1", "train_start": "2026-01-05", "train_end": "2026-06-30",
        "metrics": {"test": {"mse": 0.004, "r2": None}}, "artifact_path": "data/x",
    }


def test_model_run_row_validation() -> None:
    with pytest.raises(ValueError, match="module"):
        model_run_row(module="C", version="v", train_start=date(2026, 1, 1),  # type: ignore[arg-type]
                      train_end=date(2026, 1, 2), metrics={}, artifact_path=None)
    with pytest.raises(ValueError, match="before"):
        model_run_row(module="B", version="v", train_start=date(2026, 1, 2),
                      train_end=date(2026, 1, 1), metrics={}, artifact_path=None)


def test_record_model_run_inserts_row_untouched() -> None:
    client = FakeClient()
    row = {"module": "B", "version": "v1"}
    assert record_model_run(client, row) == 42  # type: ignore[arg-type]
    assert client.sink["table"] == "model_runs"
    assert client.sink["inserted"] == row


def test_list_model_runs_filters_and_orders() -> None:
    client = FakeClient(data=[{"id": 1}, {"id": 2}])
    assert list_model_runs(client, module="B", limit=5) == [{"id": 1}, {"id": 2}]  # type: ignore[arg-type]
    assert client.sink["eq"] == ("module", "B")
    assert client.sink["order"] == ("trained_at", True)
    assert client.sink["limit"] == 5
