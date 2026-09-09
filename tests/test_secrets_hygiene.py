"""Guards that secrets and OPRA-licensed data cannot be committed.

These are cheap, run without a database, and protect the two mistakes that are
expensive to undo in a public repo: a leaked key, and redistributed market data.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

MUST_BE_IGNORED = [
    ".env",
    ".env.local",
    "data/chains/2026-09-09.parquet",
    "snapshot.parquet",
    "models_out/module_b.pkl",
    "ibkr.key",
    "cert.pem",
]

MUST_NOT_BE_IGNORED = [
    ".env.example",
    "src/spx0dte/__init__.py",
    "supabase/migrations/0001_init_schema.sql",
]


def _is_ignored(relative_path: str) -> bool:
    """Return True if git would ignore `relative_path`."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", relative_path],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git check-ignore failed: {result.stderr.decode()}")
    return result.returncode == 0


@pytest.mark.parametrize("path", MUST_BE_IGNORED)
def test_sensitive_paths_are_gitignored(path: str) -> None:
    assert _is_ignored(path), f"{path} is NOT gitignored - it could be committed"


@pytest.mark.parametrize("path", MUST_NOT_BE_IGNORED)
def test_source_paths_are_tracked(path: str) -> None:
    assert not _is_ignored(path), f"{path} is gitignored but should be tracked"


def test_env_example_has_no_values() -> None:
    """.env.example must list keys with empty values, never real secrets."""
    example = (REPO_ROOT / ".env.example").read_text()
    offenders = []
    for line in example.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Non-secret defaults (host, port, mode) are allowed to carry a value.
        if any(token in key for token in ("KEY", "SECRET", "TOKEN", "URL", "PASSWORD")):
            if value:
                offenders.append(key)
    assert not offenders, f".env.example contains values for secret keys: {offenders}"


def test_no_env_file_is_tracked() -> None:
    """Belt and braces: no .env ever entered the index."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True
    ).stdout.splitlines()
    bad = [f for f in tracked if f == ".env" or f.startswith(".env.") and f != ".env.example"]
    assert not bad, f"secret files are tracked by git: {bad}"
