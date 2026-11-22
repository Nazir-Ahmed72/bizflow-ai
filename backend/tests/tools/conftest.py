import pytest


@pytest.fixture(autouse=True)
def _db(fresh_db):
    """Every tool test starts from a freshly seeded test branch."""
    yield
