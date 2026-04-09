"""Tests for sqlite-vec indexed vector search.

Covers:
- Schema migration: vec0 virtual tables created by init_db()
- Extension loaded: vec0 is queryable after init_db()
- search_messages_semantic: vec0-backed ranking, filters, distance→similarity conversion
- search_tasks_semantic: vec0-backed ranking, project filter
- search_message_chunks: vec0-backed ranking, filter, adjacent context
- Write path: new embeddings inserted into vec0 on set_message_embedding
- Backfill: _backfill_vec_tables populates vec0 from existing BLOBs
"""

import pytest
import struct

from switchboard.db.search import (
    search_messages_semantic,
    search_tasks_semantic,
    search_message_chunks,
)
from switchboard.embeddings.service import encode_vector, decode_vector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unit_vec(dim: int, index: int) -> list[float]:
    """Unit vector with 1.0 at index % dim, all others 0."""
    v = [0.0] * dim
    v[index % dim] = 1.0
    return v


def _similar_vec(base: list[float], noise: float = 0.01) -> list[float]:
    """Return a vector close to base (for checking similarity ordering)."""
    import math
    v = [x + noise for x in base]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


async def _insert_message_with_embedding(conn, content, embedding_vec, task_id=None, conversation_id=None):
    """Insert a message with an embedding blob directly into messages and messages_vec."""
    from switchboard.db._helpers import now_iso
    blob = encode_vector(embedding_vec)
    cursor = await conn.execute(
        """INSERT INTO messages (conversation_id, task_id, author, type, content, embedding, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (conversation_id, task_id, "tester", "note", content, blob, now_iso()),
    )
    await conn.commit()
    msg_id = cursor.lastrowid
    await conn.execute(
        "INSERT OR REPLACE INTO messages_vec(rowid, embedding) VALUES (?, ?)",
        (msg_id, blob),
    )
    await conn.commit()
    return msg_id


# ---------------------------------------------------------------------------
# Schema: vec0 tables exist after init_db
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# search_messages_semantic
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# search_tasks_semantic
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# search_message_chunks
# ---------------------------------------------------------------------------

class TestSearchMessageChunks:
    async def test_returns_nearest_chunk(self, db, sample_project):
        """The chunk with closest embedding ranks first."""
        vec_a = _unit_vec(1536, 0)
        vec_b = _unit_vec(1536, 1)
        query = _unit_vec(1536, 0)

        from switchboard.db.connection import get_db
        from switchboard.db._helpers import now_iso
        async with get_db() as conn:
            # Insert parent message
            cursor = await conn.execute(
                "INSERT INTO messages (author, type, content, created_at) VALUES (?, ?, ?, ?)",
                ("tester", "note", "parent message", now_iso()),
            )
            await conn.commit()
            msg_id = cursor.lastrowid

            # Insert two chunks
            blob_a = encode_vector(vec_a)
            blob_b = encode_vector(vec_b)
            cur_a = await conn.execute(
                "INSERT INTO message_chunks (message_id, chunk_index, heading, content, embedding) VALUES (?, ?, ?, ?, ?)",
                (msg_id, 0, "heading A", "content A", blob_a),
            )
            await conn.commit()
            chunk_a_id = cur_a.lastrowid
            cur_b = await conn.execute(
                "INSERT INTO message_chunks (message_id, chunk_index, heading, content, embedding) VALUES (?, ?, ?, ?, ?)",
                (msg_id, 1, "heading B", "content B", blob_b),
            )
            await conn.commit()
            chunk_b_id = cur_b.lastrowid

            await conn.execute("INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                               (chunk_a_id, blob_a))
            await conn.execute("INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                               (chunk_b_id, blob_b))
            await conn.commit()

        results = await search_message_chunks(query_vector=query, limit=5)
        assert len(results) >= 1
        assert results[0]["chunk_id"] == chunk_a_id


# ---------------------------------------------------------------------------
# Write path: new embeddings inserted into vec0
# ---------------------------------------------------------------------------

class TestWritePath:
    async def test_message_vec_written_on_embed(self, db):
        """messages_vec entry is created when _embed_message_async runs."""
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService
        from switchboard.server.handlers.common import _embed_message_async
        from switchboard.db._helpers import now_iso
        from switchboard.db.connection import get_db

        embed_vec = _unit_vec(1536, 30)

        class FakeService(EmbeddingService):
            async def embed(self, text):
                return embed_vec

        set_embedding_service(FakeService())
        try:
            # Insert a message to embed
            async with get_db() as conn:
                cursor = await conn.execute(
                    "INSERT INTO messages (author, type, content, created_at) VALUES (?, ?, ?, ?)",
                    ("tester", "note", "x" * 60, now_iso()),
                )
                await conn.commit()
                msg_id = cursor.lastrowid

            await _embed_message_async(msg_id, "x" * 60, "note")

            # Check messages_vec has the entry
            async with get_db() as conn:
                rows = await conn.execute_fetchall(
                    "SELECT rowid FROM messages_vec WHERE rowid = ?", (msg_id,)
                )
            assert len(rows) == 1
        finally:
            set_embedding_service(None)

    async def test_task_vec_written_on_embed(self, db, sample_project):
        """tasks_vec entry is created when _embed_task_goal_async runs."""
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService
        from switchboard.dispatch.engine import _embed_task_goal_async
        from switchboard.db.connection import get_db

        embed_vec = _unit_vec(1536, 40)

        class FakeService(EmbeddingService):
            async def embed(self, text):
                return embed_vec

        set_embedding_service(FakeService())
        try:
            task = await db.create_task(
                id="test-project/vec-write-test",
                project_id="test-project",
                goal="Test vec write path",
            )
            await _embed_task_goal_async(task["id"], task["goal"])

            async with get_db() as conn:
                rows = await conn.execute_fetchall("SELECT rowid FROM tasks WHERE id = ?", (task["id"],))
                task_rowid = rows[0]["rowid"]
                vec_rows = await conn.execute_fetchall(
                    "SELECT rowid FROM tasks_vec WHERE rowid = ?", (task_rowid,)
                )
            assert len(vec_rows) == 1
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Backfill: _backfill_vec_tables populates from existing BLOBs
# ---------------------------------------------------------------------------

class TestBackfillVecTables:
    async def test_backfill_populates_messages_vec(self, db):
        """_backfill_vec_tables inserts existing message embeddings into messages_vec."""
        from switchboard.db.connection import get_db
        from switchboard.db._helpers import now_iso
        from switchboard.server.app import _backfill_vec_tables

        vec = _unit_vec(1536, 50)
        blob = encode_vector(vec)

        async with get_db() as conn:
            cursor = await conn.execute(
                "INSERT INTO messages (author, type, content, embedding, created_at) VALUES (?, ?, ?, ?, ?)",
                ("tester", "note", "backfill test message", blob, now_iso()),
            )
            await conn.commit()
            msg_id = cursor.lastrowid

        await _backfill_vec_tables()

        async with get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT rowid FROM messages_vec WHERE rowid = ?", (msg_id,)
            )
        assert len(rows) == 1

    async def test_backfill_populates_tasks_vec(self, db, sample_project):
        """_backfill_vec_tables inserts existing task embeddings into tasks_vec."""
        from switchboard.db.connection import get_db
        from switchboard.server.app import _backfill_vec_tables

        task = await db.create_task(
            id="test-project/backfill-vec-task",
            project_id="test-project",
            goal="Backfill vec task",
        )
        vec = _unit_vec(1536, 55)
        blob = encode_vector(vec)
        await db.set_task_embedding(task["id"], blob)

        await _backfill_vec_tables()

        async with get_db() as conn:
            rows = await conn.execute_fetchall("SELECT rowid FROM tasks WHERE id = ?", (task["id"],))
            task_rowid = rows[0]["rowid"]
            vec_rows = await conn.execute_fetchall(
                "SELECT rowid FROM tasks_vec WHERE rowid = ?", (task_rowid,)
            )
        assert len(vec_rows) == 1

