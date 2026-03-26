"""Database connection management — singleton with async context manager."""
import aiosqlite
from contextlib import asynccontextmanager

from switchboard.config.settings import DB_PATH

_connection: aiosqlite.Connection | None = None


async def _get_shared_connection() -> aiosqlite.Connection:
    """Get or create the shared database connection. Sets PRAGMAs once."""
    global _connection
    if _connection is None:
        _connection = await aiosqlite.connect(DB_PATH)
        _connection.row_factory = aiosqlite.Row
        await _connection.execute("PRAGMA journal_mode=WAL")
        await _connection.execute("PRAGMA foreign_keys=ON")
    return _connection


@asynccontextmanager
async def get_db():
    """Async context manager that yields the shared connection."""
    db = await _get_shared_connection()
    yield db


async def close_db():
    """Close the shared connection. Call on shutdown."""
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None
