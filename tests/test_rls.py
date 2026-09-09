"""Verifies the database refuses to hand OPRA-licensed quote columns to `anon`.

Requires SUPABASE_DB_URL. Skipped when it is unset, so the suite still runs
offline. This is the test that protects the market-data licence -- if it fails,
the public dashboard is redistributing raw quotes.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

psycopg = pytest.importorskip("psycopg")

load_dotenv()
DB_URL = os.environ.get("SUPABASE_DB_URL")

pytestmark = pytest.mark.skipif(
    not DB_URL, reason="SUPABASE_DB_URL not set; skipping live database tests"
)

RESTRICTED_COLUMNS = ["bid", "ask", "mid"]
PRIVATE_TABLES = [
    "snapshots_meta",
    "surface_fits",
    "model_runs",
    "paper_trades",
    "risk_state",
]


@pytest.fixture()
def anon_cursor():
    """A cursor running as the `anon` role, rolled back after each test."""
    with psycopg.connect(DB_URL) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("set local role anon")
            yield cur
        conn.rollback()


@pytest.mark.parametrize("column", RESTRICTED_COLUMNS)
def test_anon_cannot_read_raw_quote_columns(anon_cursor, column: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        anon_cursor.execute(f"select {column} from public.signals limit 1")


def test_anon_cannot_select_star_from_signals(anon_cursor) -> None:
    """`select *` expands to every column, including the restricted ones."""
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        anon_cursor.execute("select * from public.signals limit 1")


def test_anon_can_read_derived_signal_columns(anon_cursor) -> None:
    anon_cursor.execute(
        "select ts, module, edge_after_costs, confidence, action "
        "from public.signals limit 1"
    )


def test_anon_can_read_live_signals_view(anon_cursor) -> None:
    anon_cursor.execute("select * from public.v_live_signals limit 1")


def test_anon_can_read_equity_curve_view(anon_cursor) -> None:
    anon_cursor.execute("select * from public.v_equity_curve limit 1")


@pytest.mark.parametrize("table", PRIVATE_TABLES)
def test_anon_cannot_read_private_tables(anon_cursor, table: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        anon_cursor.execute(f"select * from public.{table} limit 1")


@pytest.mark.parametrize("table", ["signals", "daily_metrics", "risk_state"])
def test_anon_cannot_write(anon_cursor, table: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        anon_cursor.execute(f"delete from public.{table}")
