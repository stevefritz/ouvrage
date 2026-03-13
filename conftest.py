"""Shared fixtures for switchboard tests."""

import os
import sys
import tempfile

import pytest

# Ensure the project root is on sys.path so `import database` etc. works
sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture
def tmp_db(tmp_path):
    """Point database.py at a temporary SQLite file and reset the singleton."""
    db_path = str(tmp_path / "test.db")
    os.environ["SWITCHBOARD_DB"] = db_path

    import database as db
    # Reset singleton so it picks up the new path
    db.DB_PATH = db_path
    db._connection = None
    return db_path


@pytest.fixture
async def db(tmp_db):
    """Initialized database ready for use. Yields the database module."""
    import database as _db
    await _db.init_db()
    yield _db
    await _db.close_db()
    _db._connection = None
