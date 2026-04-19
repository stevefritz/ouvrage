"""Tests for vec0 resilience: VEC_AVAILABLE flag, delete triggers, reconciliation, try/except safety."""

import struct
from unittest.mock import AsyncMock, patch

import pytest

from ouvrage.db.search import (
    search_messages_semantic,
    search_tasks_semantic,
    search_message_chunks,
)
from ouvrage.embeddings.service import encode_vector


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
        import ouvrage.db.search as search_mod
        from ouvrage.db.search import _check_vec_tables

        await _check_vec_tables()
        assert search_mod.VEC_AVAILABLE is True

    async def test_check_vec_tables_sets_false_on_error(self, db):
        """_check_vec_tables() sets VEC_AVAILABLE=False when query raises."""
        import ouvrage.db.search as search_mod
        from ouvrage.db.search import _check_vec_tables
        from ouvrage.db.connection import get_db

        # Save state and restore after
        original = search_mod.VEC_AVAILABLE
        try:
            # Patch execute_fetchall to raise
            with patch("ouvrage.db.search.get_db") as mock_get_db:
                mock_conn = AsyncMock()
                mock_conn.execute_fetchall.side_effect = Exception("no such table")
                mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
                mock_conn.__aexit__ = AsyncMock(return_value=False)
                mock_get_db.return_value = mock_conn

                await _check_vec_tables()
                assert search_mod.VEC_AVAILABLE is False
        finally:
            search_mod.VEC_AVAILABLE = original

    async def test_handler_uses_fts_only_when_vec_unavailable(self, db, sample_project):
        """_handle_search falls back to FTS-only when VEC_AVAILABLE is False."""
        import ouvrage.db.search as search_mod
        from ouvrage.server.handlers.search import _handle_search
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService

        vec = _unit_vec(1536, 100)

        class FakeService(EmbeddingService):
            async def embed(self, text):
                return vec

        original_vec_available = search_mod.VEC_AVAILABLE
        set_embedding_service(FakeService())
        try:
            search_mod.VEC_AVAILABLE = False
            # Should not raise even though vec tables would fail if queried
            result = await _handle_search({"query": "test query"})
            assert "results" in result
            assert "total_candidates" in result
        finally:
            search_mod.VEC_AVAILABLE = original_vec_available
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Delete triggers: messages_vec
# ---------------------------------------------------------------------------

class TestMessagesVecDeleteTrigger:
    async def test_delete_message_removes_vec_entry(self, db):
        """Deleting a message removes the corresponding messages_vec row."""
        from ouvrage.db.connection import get_db
        from ouvrage.db._helpers import now_iso

        vec = _unit_vec(1536, 200)
        blob = encode_vector(vec)

        async with get_db() as conn:
            cursor = await conn.execute(
                "INSERT INTO messages (author, type, content, embedding, created_at) VALUES (?, ?, ?, ?, ?)",
                ("tester", "note", "to be deleted", blob, now_iso()),
            )
            await conn.commit()
            msg_id = cursor.lastrowid

            # Insert into messages_vec
            await conn.execute(
                "INSERT OR REPLACE INTO messages_vec(rowid, embedding) VALUES (?, ?)",
                (msg_id, blob),
            )
            await conn.commit()

            # Verify vec row exists
            rows = await conn.execute_fetchall(
                "SELECT rowid FROM messages_vec WHERE rowid = ?", (msg_id,)
            )
            assert len(rows) == 1, "Pre-condition: vec row should exist"

            # Delete the message
            await conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
            await conn.commit()

            # Verify vec row was cleaned up by trigger
            rows_after = await conn.execute_fetchall(
                "SELECT rowid FROM messages_vec WHERE rowid = ?", (msg_id,)
            )
        assert len(rows_after) == 0, "messages_vec row should be deleted by trigger"

    async def test_delete_nonexistent_message_no_error(self, db):
        """Deleting a message with no vec entry doesn't error."""
        from ouvrage.db.connection import get_db
        from ouvrage.db._helpers import now_iso

        async with get_db() as conn:
            cursor = await conn.execute(
                "INSERT INTO messages (author, type, content, created_at) VALUES (?, ?, ?, ?)",
                ("tester", "note", "no vec here", now_iso()),
            )
            await conn.commit()
            msg_id = cursor.lastrowid

            # No vec entry was inserted
            await conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
            await conn.commit()
            # Should not raise


# ---------------------------------------------------------------------------
# Delete triggers: tasks_vec
# ---------------------------------------------------------------------------

class TestTasksVecDeleteTrigger:
    async def test_delete_task_removes_vec_entry(self, db, sample_project):
        """Deleting a task removes the corresponding tasks_vec row."""
        from ouvrage.db.connection import get_db

        task = await db.create_task(
            id="test-project/vec-delete-test",
            project_id="test-project",
            goal="Task to be deleted",
        )

        vec = _unit_vec(1536, 201)
        blob = encode_vector(vec)

        async with get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT rowid FROM tasks WHERE id = ?", (task["id"],)
            )
            task_rowid = rows[0]["rowid"]

            await conn.execute(
                "INSERT OR REPLACE INTO tasks_vec(rowid, embedding) VALUES (?, ?)",
                (task_rowid, blob),
            )
            await conn.commit()

            # Verify vec row exists
            vec_rows = await conn.execute_fetchall(
                "SELECT rowid FROM tasks_vec WHERE rowid = ?", (task_rowid,)
            )
            assert len(vec_rows) == 1

            # Delete the task (need to remove checklist first due to FK, or rely on CASCADE)
            # tasks don't have ON DELETE CASCADE for checklist, but that's fine — just delete task
            await conn.execute("DELETE FROM task_checklist WHERE task_id = ?", (task["id"],))
            await conn.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
            await conn.commit()

            # Verify vec row was cleaned up
            vec_rows_after = await conn.execute_fetchall(
                "SELECT rowid FROM tasks_vec WHERE rowid = ?", (task_rowid,)
            )
        assert len(vec_rows_after) == 0, "tasks_vec row should be deleted by trigger"


# ---------------------------------------------------------------------------
# Delete triggers: chunks_vec
# ---------------------------------------------------------------------------

class TestChunksVecDeleteTrigger:
    async def test_delete_chunk_removes_vec_entry(self, db):
        """Deleting a message_chunk removes the corresponding chunks_vec row."""
        from ouvrage.db.connection import get_db
        from ouvrage.db._helpers import now_iso

        vec = _unit_vec(1536, 202)
        blob = encode_vector(vec)

        async with get_db() as conn:
            # Insert parent message
            cursor = await conn.execute(
                "INSERT INTO messages (author, type, content, created_at) VALUES (?, ?, ?, ?)",
                ("tester", "note", "parent", now_iso()),
            )
            await conn.commit()
            msg_id = cursor.lastrowid

            # Insert chunk
            cur_c = await conn.execute(
                "INSERT INTO message_chunks (message_id, chunk_index, heading, content, embedding) VALUES (?, ?, ?, ?, ?)",
                (msg_id, 0, "h", "content", blob),
            )
            await conn.commit()
            chunk_id = cur_c.lastrowid

            # Insert into chunks_vec
            await conn.execute(
                "INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                (chunk_id, blob),
            )
            await conn.commit()

            # Verify vec row exists
            rows = await conn.execute_fetchall(
                "SELECT rowid FROM chunks_vec WHERE rowid = ?", (chunk_id,)
            )
            assert len(rows) == 1

            # Delete the chunk directly
            await conn.execute("DELETE FROM message_chunks WHERE id = ?", (chunk_id,))
            await conn.commit()

            # Verify vec row was cleaned up
            rows_after = await conn.execute_fetchall(
                "SELECT rowid FROM chunks_vec WHERE rowid = ?", (chunk_id,)
            )
        assert len(rows_after) == 0, "chunks_vec row should be deleted by trigger"

    async def test_delete_message_cascade_removes_chunk_vec_entries(self, db):
        """Deleting a message cascades to message_chunks, which fires chunks_vec_delete trigger."""
        from ouvrage.db.connection import get_db
        from ouvrage.db._helpers import now_iso

        vec = _unit_vec(1536, 203)
        blob = encode_vector(vec)

        async with get_db() as conn:
            # Insert parent message
            cursor = await conn.execute(
                "INSERT INTO messages (author, type, content, created_at) VALUES (?, ?, ?, ?)",
                ("tester", "note", "parent for cascade", now_iso()),
            )
            await conn.commit()
            msg_id = cursor.lastrowid

            # Insert chunk
            cur_c = await conn.execute(
                "INSERT INTO message_chunks (message_id, chunk_index, heading, content, embedding) VALUES (?, ?, ?, ?, ?)",
                (msg_id, 0, "h", "chunk content for cascade test", blob),
            )
            await conn.commit()
            chunk_id = cur_c.lastrowid

            # Insert into chunks_vec
            await conn.execute(
                "INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                (chunk_id, blob),
            )
            await conn.commit()

            # Verify vec row exists
            rows = await conn.execute_fetchall(
                "SELECT rowid FROM chunks_vec WHERE rowid = ?", (chunk_id,)
            )
            assert len(rows) == 1, "Pre-condition: chunk_vec row should exist"

            # Delete the parent message — should cascade to message_chunks,
            # which fires chunks_vec_delete trigger
            await conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
            await conn.commit()

            # Verify chunks_vec row was cleaned up via CASCADE → trigger
            rows_after = await conn.execute_fetchall(
                "SELECT rowid FROM chunks_vec WHERE rowid = ?", (chunk_id,)
            )
        assert len(rows_after) == 0, "chunks_vec row should be deleted via CASCADE trigger"


# ---------------------------------------------------------------------------
# Orphan reconciliation
# ---------------------------------------------------------------------------

class TestOrphanReconciliation:
    async def test_reconciliation_removes_orphan_messages_vec(self, db):
        """_backfill_vec_tables() prunes messages_vec rows with no matching message."""
        from ouvrage.db.connection import get_db
        from ouvrage.server.app import _backfill_vec_tables

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

    async def test_reconciliation_removes_orphan_tasks_vec(self, db, sample_project):
        """_backfill_vec_tables() prunes tasks_vec rows with no matching task."""
        from ouvrage.db.connection import get_db
        from ouvrage.server.app import _backfill_vec_tables

        vec = _unit_vec(1536, 301)
        blob = encode_vector(vec)

        async with get_db() as conn:
            orphan_rowid = 999998
            await conn.execute(
                "INSERT OR REPLACE INTO tasks_vec(rowid, embedding) VALUES (?, ?)",
                (orphan_rowid, blob),
            )
            await conn.commit()

            rows = await conn.execute_fetchall(
                "SELECT rowid FROM tasks_vec WHERE rowid = ?", (orphan_rowid,)
            )
            assert len(rows) == 1

        await _backfill_vec_tables()

        async with get_db() as conn:
            rows_after = await conn.execute_fetchall(
                "SELECT rowid FROM tasks_vec WHERE rowid = ?", (orphan_rowid,)
            )
        assert len(rows_after) == 0, "Orphan tasks_vec row should be pruned by reconciliation"

    async def test_reconciliation_removes_orphan_chunks_vec(self, db):
        """_backfill_vec_tables() prunes chunks_vec rows with no matching message_chunk."""
        from ouvrage.db.connection import get_db
        from ouvrage.server.app import _backfill_vec_tables

        vec = _unit_vec(1536, 302)
        blob = encode_vector(vec)

        async with get_db() as conn:
            orphan_rowid = 999997
            await conn.execute(
                "INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                (orphan_rowid, blob),
            )
            await conn.commit()

            rows = await conn.execute_fetchall(
                "SELECT rowid FROM chunks_vec WHERE rowid = ?", (orphan_rowid,)
            )
            assert len(rows) == 1

        await _backfill_vec_tables()

        async with get_db() as conn:
            rows_after = await conn.execute_fetchall(
                "SELECT rowid FROM chunks_vec WHERE rowid = ?", (orphan_rowid,)
            )
        assert len(rows_after) == 0, "Orphan chunks_vec row should be pruned by reconciliation"

    async def test_reconciliation_keeps_valid_vec_entries(self, db):
        """_backfill_vec_tables() does not remove vec entries that have matching messages."""
        from ouvrage.db.connection import get_db
        from ouvrage.db._helpers import now_iso
        from ouvrage.server.app import _backfill_vec_tables

        vec = _unit_vec(1536, 303)
        blob = encode_vector(vec)

        async with get_db() as conn:
            cursor = await conn.execute(
                "INSERT INTO messages (author, type, content, embedding, created_at) VALUES (?, ?, ?, ?, ?)",
                ("tester", "note", "valid message", blob, now_iso()),
            )
            await conn.commit()
            msg_id = cursor.lastrowid

            await conn.execute(
                "INSERT OR REPLACE INTO messages_vec(rowid, embedding) VALUES (?, ?)",
                (msg_id, blob),
            )
            await conn.commit()

        await _backfill_vec_tables()

        async with get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT rowid FROM messages_vec WHERE rowid = ?", (msg_id,)
            )
        assert len(rows) == 1, "Valid vec entry should survive reconciliation"


# ---------------------------------------------------------------------------
# try/except safety net: vec0 queries return [] on OperationalError
# ---------------------------------------------------------------------------

class TestVecQuerySafetyNet:
    async def test_search_messages_semantic_returns_empty_on_vec_error(self, db):
        """search_messages_semantic returns [] when the vec0 MATCH query raises."""
        from aiosqlite import OperationalError

        vec = _unit_vec(1536, 400)

        with patch("ouvrage.db.search.get_db") as mock_get_db:
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

        with patch("ouvrage.db.search.get_db") as mock_get_db:
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

        with patch("ouvrage.db.search.get_db") as mock_get_db:
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
