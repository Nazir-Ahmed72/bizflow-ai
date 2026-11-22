"""Settings loaded from backend/.env (or the process environment)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BACKEND_DIR / ".env")


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is not set. Copy backend/.env.example to backend/.env and fill it in.")
    return value


def database_url() -> str:
    """SQLAlchemy URL using the psycopg 3 driver.

    Accepts Neon's string as copied (postgresql://... or postgres://...) and
    rewrites the scheme to postgresql+psycopg:// so psycopg2 is never needed.
    """
    url = _require("TEST_DATABASE_URL" if os.getenv("USE_TEST_DB") == "1" else "DATABASE_URL").strip()
    # Neon: always use the direct endpoint. PgBouncer (transaction pooling) breaks
    # Alembic's transactional DDL and LangGraph's checkpointer.
    url = url.replace("-pooler.", ".", 1)
    for prefix in ("postgresql+psycopg://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    raise RuntimeError("DATABASE_URL must start with postgresql:// (or postgres://)")


def psycopg_url() -> str:
    """Plain libpq URL (no '+psycopg') for psycopg / the LangGraph checkpointer."""
    return database_url().replace("postgresql+psycopg://", "postgresql://", 1)


GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")


def groq_api_key() -> str:
    return _require("GROQ_API_KEY")
