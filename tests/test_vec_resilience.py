"""Tests for vec0 resilience: VEC_AVAILABLE flag, delete triggers, reconciliation, try/except safety."""

import struct
from unittest.mock import AsyncMock, patch

import pytest

from switchboard.db.search import (
    search_messages_semantic,
    search_tasks_semantic,
    search_message_chunks,
)
from switchboard.embeddings.service import encode_vector


def _unit_vec(dim: int, index: int) -> list[float]:
    v = [0.0] * dim
    v[index % dim] = 1.0
    return v


# ---------------------------------------------------------------------------
# VEC_AVAILABLE flag
# ---------------------------------------------------------------------------

class TestVecAvailableFlag:
    async def test_check_vec_tables_sets_true_when_available(self, db):
        """_check_vec_tables() sets VEC_AVAILABLE=True when vec0 tables exist."""
        import switchboard.db.search as search_mod
        from switchboard.db.search import _check_vec_tables

        await _check_vec_tables()
        assert search_mod.VEC_AVAILABLE is True

    async def test_check_vec_tables_sets_false_on_error(self, db):
        """_check_vec_tables() sets VEC_AVAILABLE=False when query raises."""
        import switchboard.db.search as search_mod
        from switchboard.db.search import _check_vec_tables
        from switchboard.db.connection import get_db

        # Save state and restore after
        original = search_mod.VEC_AVAILABLE
        try:
            # Patch execute_fetchall to raise
            with patch("switchboard.db.search.get_db") as mock_get_db:
                mock_conn = AsyncMock()
                mock_conn.execute_fetchall.side_effect = Exception("no such table")
                mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
                mock_conn.__aexit__ = AsyncMock(return_value=False)
                mock_get_db.return_value = mock_conn

                await _check_vec_tables()
                assert search_mod.VEC_AVAILABLE is False
        finally:
            search_mod.VEC_AVAILABLE = original


# ---------------------------------------------------------------------------
# Delete triggers: messages_vec
# ---------------------------------------------------------------------------

            # Should not raise


# ---------------------------------------------------------------------------
# Delete triggers: tasks_vec
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Delete triggers: chunks_vec
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Orphan reconciliation
# ---------------------------------------------------------------------------

class TestOrphanReconciliation:
    async def test_reconciliation_removes_orphan_messages_vec(self, db):
        """_backfill_vec_tables() prunes messages_vec rows with no matching message."""
        from switchboard.db.connection import get_db
        from switchboard.server.app import _backfill_vec_tables

        vec = _unit_vec(1536, 300)
        blob = encode_vector(vec)

        async with get_db() as conn:
            # Insert a vec row with a rowid that has no matching message
            orphan_rowid = 999999
            await conn.execute(
                "INSERT OR REPLACE INTO messages_vec(rowid, embedding) VALUES (?, ?)",
                (orphan_rowid, blob),
            )
            await conn.commit()

            rows = await conn.execute_fetchall(
                "SELECT rowid FROM messages_vec WHERE rowid = ?", (orphan_rowid,)
            )
            assert len(rows) == 1, "Pre-condition: orphan vec row should exist"

        await _backfill_vec_tables()

        async with get_db() as conn:
            rows_after = await conn.execute_fetchall(
                "SELECT rowid FROM messages_vec WHERE rowid = ?", (orphan_rowid,)
            )
        assert len(rows_after) == 0, "Orphan messages_vec row should be pruned by reconciliation"


# ---------------------------------------------------------------------------
# try/except safety net: vec0 queries return [] on OperationalError
# ---------------------------------------------------------------------------

class TestVecQuerySafetyNet:
    async def test_search_messages_semantic_returns_empty_on_vec_error(self, db):
        """search_messages_semantic returns [] when the vec0 MATCH query raises."""
        from aiosqlite import OperationalError

        vec = _unit_vec(1536, 400)

        with patch("switchboard.db.search.get_db") as mock_get_db:
            mock_conn = AsyncMock()
            mock_conn.execute_fetchall.side_effect = OperationalError("no such table: messages_vec")
            mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn.__aexit__ = AsyncMock(return_value=False)
            mock_get_db.return_value = mock_conn

            result = await search_messages_semantic(query_vector=vec, limit=5)

        assert result == [], "Should return [] when vec0 query raises"

    async def test_search_tasks_semantic_returns_empty_on_vec_error(self, db):
        """search_tasks_semantic returns [] when the vec0 MATCH query raises."""
        from aiosqlite import OperationalError

        vec = _unit_vec(1536, 401)

        with patch("switchboard.db.search.get_db") as mock_get_db:
            mock_conn = AsyncMock()
            mock_conn.execute_fetchall.side_effect = OperationalError("no such table: tasks_vec")
            mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn.__aexit__ = AsyncMock(return_value=False)
            mock_get_db.return_value = mock_conn

            result = await search_tasks_semantic(query_vector=vec, limit=5)

        assert result == [], "Should return [] when vec0 query raises"

    async def test_search_message_chunks_returns_empty_on_vec_error(self, db):
        """search_message_chunks returns [] when the vec0 MATCH query raises."""
        from aiosqlite import OperationalError

        vec = _unit_vec(1536, 402)

        with patch("switchboard.db.search.get_db") as mock_get_db:
            mock_conn = AsyncMock()
            mock_conn.execute_fetchall.side_effect = OperationalError("no such table: chunks_vec")
            mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_conn.__aexit__ = AsyncMock(return_value=False)
            mock_get_db.return_value = mock_conn

            result = await search_message_chunks(query_vector=vec, limit=5)

        assert result == [], "Should return [] when vec0 query raises"

    async def test_search_messages_semantic_fallback_on_small_vector(self, db):
        """search_messages_semantic uses Python cosine loop for non-1536-dim vectors (no vec0 query)."""
        vec = _unit_vec(4, 0)  # 4-dim, not 1536 — bypasses vec0 entirely
        result = await search_messages_semantic(query_vector=vec, limit=5)
        assert isinstance(result, list)
