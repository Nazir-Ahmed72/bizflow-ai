"""Point the whole test run at the Neon *test branch* before any app module connects.

DB tests require TEST_DATABASE_URL in backend/.env (a Neon branch). They are skipped,
never run against DATABASE_URL, when it is missing.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from dotenv import dotenv_values

_env = {**dotenv_values(Path(__file__).resolve().parent.parent / ".env"), **os.environ}
TEST_DB_URL = _env.get("TEST_DATABASE_URL")
_MAIN_DB_URL = _env.get("DATABASE_URL")

if TEST_DB_URL:
    if _MAIN_DB_URL and urlparse(TEST_DB_URL.replace("-pooler.", ".")).hostname == urlparse(
        _MAIN_DB_URL.replace("-pooler.", ".")
    ).hostname:
        raise RuntimeError("TEST_DATABASE_URL points at the same Neon endpoint as DATABASE_URL; refusing to run.")
    os.environ["DATABASE_URL"] = TEST_DB_URL  # app.config's load_dotenv() won't override this
else:
    os.environ.pop("DATABASE_URL", None)
    os.environ["DATABASE_URL"] = "postgresql://unset-test-db.invalid/none"  # fail fast, never main DB


@pytest.fixture(scope="session")
def migrated_db():
    """Apply Alembic migrations to the test branch once per run."""
    if not TEST_DB_URL:
        pytest.skip("TEST_DATABASE_URL not set (Neon test branch)")
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config(str(Path(__file__).resolve().parent.parent / "alembic.ini")), "head")
    yield


@pytest.fixture
def fresh_db(migrated_db):
    """Wipe transactional data + conversation state and restore seed stock before the test."""
    from app.db.seed import seed

    seed(reset=True)
    yield


# --------------------------------------------------------------------------- shared helpers

ALI = "+92 300 1234567"
BISMILLAH = "+92 321 5550192"
KARACHI_MART = "+92 345 2201876"
MADINA = "+92 312 4478210"
UNKNOWN = "+92 333 9876543"


@pytest.fixture
def sql():
    """Run a raw query on the test DB (tests may inspect the DB; agent code may not)."""
    from sqlalchemy import text

    from app.db.session import get_engine

    def run(query: str, **params):
        with get_engine().begin() as conn:
            result = conn.execute(text(query), params)
            return result.all() if result.returns_rows else result.rowcount

    return run


@pytest.fixture
def stock(sql):
    """stock('P-001') -> (physical, reserved)"""
    def get(pid: str) -> tuple[int, int]:
        (row,) = sql("SELECT physical, reserved FROM inventory WHERE product_id = :p", p=pid)
        return row[0], row[1]

    return get


@pytest.fixture
def customer_id():
    from app.tools import identify_customer

    def get(phone: str) -> int:
        res = identify_customer(phone)
        assert res.success, res
        return res.customer_id

    return get
