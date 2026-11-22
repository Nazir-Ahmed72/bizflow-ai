"""Engine and session factory. Only app.tools (and seed/migrations) should import this."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import database_url


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    # pool_pre_ping: Neon suspends idle computes, so stale connections are common.
    return create_engine(database_url(), pool_pre_ping=True, pool_recycle=300)


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """One transaction: commit on success, roll back on any exception."""
    session = _session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
