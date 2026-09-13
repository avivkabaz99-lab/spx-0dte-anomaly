"""Supabase access for derived data. Raw quotes never come through here.

The service-role key bypasses RLS, so this client is for backend writes on
this machine only. The key is read from the environment by `SupabaseConfig`
and never logged.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Literal

import numpy as np

from spx0dte.config import SupabaseConfig
from supabase import Client, create_client


def supabase_client(config: SupabaseConfig | None = None) -> Client:
    config = config or SupabaseConfig.from_env()
    return create_client(config.url, config.service_role_key)


def jsonable(value: Any) -> Any:
    """Recursively turn numpy scalars, dates and NaN into what jsonb accepts."""
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def model_run_row(
    *,
    module: Literal["A", "B"],
    version: str,
    train_start: date,
    train_end: date,
    metrics: Mapping[str, Any],
    artifact_path: str | None,
) -> dict[str, Any]:
    """A `model_runs` row, pure and serialisable."""
    if module not in ("A", "B"):
        raise ValueError(f"module must be 'A' or 'B', got {module!r}")
    if train_end < train_start:
        raise ValueError(f"train_end {train_end} before train_start {train_start}")
    return {
        "module": module,
        "version": version,
        "train_start": train_start.isoformat(),
        "train_end": train_end.isoformat(),
        "metrics": jsonable(metrics),
        "artifact_path": artifact_path,
    }


def record_model_run(client: Client, row: Mapping[str, Any]) -> int:
    """Insert one `model_runs` row and return its id."""
    response = client.table("model_runs").insert(dict(row)).execute()
    return int(response.data[0]["id"])


def list_model_runs(client: Client, module: str = "B", limit: int = 20) -> list[dict[str, Any]]:
    """Most recent runs for a module, newest first."""
    response = (
        client.table("model_runs")
        .select("id, module, version, trained_at, train_start, train_end, metrics, artifact_path")
        .eq("module", module)
        .order("trained_at", desc=True)
        .limit(limit)
        .execute()
    )
    return list(response.data)
