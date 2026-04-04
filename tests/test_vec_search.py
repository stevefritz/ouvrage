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

class TestVec0Schema:
    async def test_messages_vec_table_exists(self, db):
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_vec'"
            )
        assert len(rows) == 1

    async def test_tasks_vec_table_exists(self, db):
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks_vec'"
            )
        assert len(rows) == 1

    async def test_chunks_vec_table_exists(self, db):
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
            )
        assert len(rows) == 1

    async def test_vec0_is_queryable(self, db):
        """sqlite-vec extension is loaded and vec0 tables accept MATCH queries."""
        from switchboard.db.connection import get_db
        blob = encode_vector([0.0] * 1536)
        async with get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT rowid, distance FROM messages_vec WHERE embedding MATCH ? ORDER BY distance LIMIT 5",
                (blob,),
            )
        assert rows == []  # empty table, no results — just confirming no error


# ---------------------------------------------------------------------------
# search_messages_semantic
# ---------------------------------------------------------------------------

class TestSearchMessagesSemantic:
    async def test_returns_nearest_neighbor(self, db, sample_project):
        """The message with the closest embedding ranks first."""
        vec_a = _unit_vec(1536, 0)
        vec_b = _unit_vec(1536, 1)
        query = _unit_vec(1536, 0)  # identical to vec_a

        from switchboard.db.connection import get_db
        async with get_db() as conn:
            id_a = await _insert_message_with_embedding(conn, "message A", vec_a)
            id_b = await _insert_message_with_embedding(conn, "message B", vec_b)

        results = await search_messages_semantic(query_vector=query, limit=10)
        ids = [r["message_id"] for r in results]
        assert id_a in ids
        assert results[0]["message_id"] == id_a

    async def test_similarity_in_range(self, db):
        """All similarity scores are in [0, 1]."""
        vec = _unit_vec(1536, 5)
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            await _insert_message_with_embedding(conn, "test message", vec)

        results = await search_messages_semantic(query_vector=vec, limit=10)
        for r in results:
            assert 0.0 <= r["similarity"] <= 1.0

    async def test_exact_match_similarity_near_one(self, db):
        """Querying with the same vector gives similarity close to 1.0."""
        vec = _unit_vec(1536, 3)
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            msg_id = await _insert_message_with_embedding(conn, "exact match message", vec)

        results = await search_messages_semantic(query_vector=vec, limit=5)
        hits = [r for r in results if r["message_id"] == msg_id]
        assert hits, "Expected the exact-match message in results"
        assert hits[0]["similarity"] > 0.99

    async def test_returns_expected_fields(self, db):
        """Result dicts contain all required fields."""
        vec = _unit_vec(1536, 7)
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            await _insert_message_with_embedding(conn, "field check message", vec)

        results = await search_messages_semantic(query_vector=vec, limit=5)
        assert len(results) >= 1
        r = results[0]
        for field in ("message_id", "conversation_id", "task_id", "author", "type",
                      "title", "content", "pinned", "created_at", "similarity"):
            assert field in r, f"Missing field: {field}"

    async def test_filters_by_conversation_id(self, db, sample_conversation):
        """conversation_id filter scopes results."""
        vec = _unit_vec(1536, 10)
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            # Message in the sample_conversation
            in_id = await _insert_message_with_embedding(
                conn, "in conversation", vec, conversation_id=sample_conversation["id"]
            )
            # Message in no conversation
            out_id = await _insert_message_with_embedding(conn, "no conversation", vec)

        results = await search_messages_semantic(
            query_vector=vec, conversation_id=sample_conversation["id"], limit=20
        )
        ids = [r["message_id"] for r in results]
        assert in_id in ids
        assert out_id not in ids

    async def test_filters_by_project_id(self, db, sample_project, sample_task):
        """project_id filter scopes results to messages linked to that project."""
        vec = _unit_vec(1536, 12)
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            in_id = await _insert_message_with_embedding(
                conn, "task message in project", vec, task_id=sample_task["id"]
            )
            out_id = await _insert_message_with_embedding(conn, "orphan message", vec)

        results = await search_messages_semantic(
            query_vector=vec, project_id=sample_project["id"], limit=20
        )
        ids = [r["message_id"] for r in results]
        assert in_id in ids
        assert out_id not in ids

    async def test_empty_when_no_vec0_entries(self, db):
        """Returns empty list when vec0 table is empty."""
        vec = _unit_vec(1536, 2)
        results = await search_messages_semantic(query_vector=vec, limit=5)
        assert results == []

    async def test_returns_up_to_limit_times_three(self, db):
        """Returns at most limit*3 results for caller re-ranking."""
        vec = _unit_vec(1536, 4)
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            for i in range(20):
                await _insert_message_with_embedding(conn, f"message {i}", vec)

        results = await search_messages_semantic(query_vector=vec, limit=5)
        assert len(results) <= 15  # limit*3


# ---------------------------------------------------------------------------
# search_tasks_semantic
# ---------------------------------------------------------------------------

class TestSearchTasksSemantic:
    async def test_returns_nearest_task(self, db, sample_project):
        """The task with the closest embedding ranks first."""
        vec_a = _unit_vec(1536, 0)
        vec_b = _unit_vec(1536, 1)
        query = _unit_vec(1536, 0)

        task_a = await db.create_task(
            id="test-project/vec-task-a", project_id="test-project", goal="Task A"
        )
        task_b = await db.create_task(
            id="test-project/vec-task-b", project_id="test-project", goal="Task B"
        )

        from switchboard.db.connection import get_db
        async with get_db() as conn:
            rows_a = await conn.execute_fetchall("SELECT rowid FROM tasks WHERE id = ?", (task_a["id"],))
            rows_b = await conn.execute_fetchall("SELECT rowid FROM tasks WHERE id = ?", (task_b["id"],))
            await conn.execute("INSERT OR REPLACE INTO tasks_vec(rowid, embedding) VALUES (?, ?)",
                               (rows_a[0]["rowid"], encode_vector(vec_a)))
            await conn.execute("INSERT OR REPLACE INTO tasks_vec(rowid, embedding) VALUES (?, ?)",
                               (rows_b[0]["rowid"], encode_vector(vec_b)))
            await conn.commit()

        results = await search_tasks_semantic(query_vector=query, limit=10)
        assert len(results) >= 1
        assert results[0]["task_id"] == task_a["id"]

    async def test_returns_expected_fields(self, db, sample_project):
        """Result dicts contain all required fields."""
        vec = _unit_vec(1536, 8)
        task = await db.create_task(
            id="test-project/vec-shape", project_id="test-project", goal="Shape test"
        )
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            rows = await conn.execute_fetchall("SELECT rowid FROM tasks WHERE id = ?", (task["id"],))
            await conn.execute("INSERT OR REPLACE INTO tasks_vec(rowid, embedding) VALUES (?, ?)",
                               (rows[0]["rowid"], encode_vector(vec)))
            await conn.commit()

        results = await search_tasks_semantic(query_vector=vec, limit=5)
        assert len(results) >= 1
        r = results[0]
        for field in ("task_id", "project_id", "goal", "status", "created_at", "similarity"):
            assert field in r

    async def test_similarity_near_one_for_exact_match(self, db, sample_project):
        """Exact-match query gives similarity close to 1.0."""
        vec = _unit_vec(1536, 20)
        task = await db.create_task(
            id="test-project/vec-exact", project_id="test-project", goal="Exact task"
        )
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            rows = await conn.execute_fetchall("SELECT rowid FROM tasks WHERE id = ?", (task["id"],))
            await conn.execute("INSERT OR REPLACE INTO tasks_vec(rowid, embedding) VALUES (?, ?)",
                               (rows[0]["rowid"], encode_vector(vec)))
            await conn.commit()

        results = await search_tasks_semantic(query_vector=vec, limit=5)
        hits = [r for r in results if r["task_id"] == task["id"]]
        assert hits
        assert hits[0]["similarity"] > 0.99

    async def test_filters_by_project_id(self, db, sample_project):
        """project_id filter excludes tasks from other projects."""
        await db.create_project(
            id="other-proj", repo="https://github.com/x/y.git",
            working_dir="/work/y", default_branch="main"
        )
        vec = _unit_vec(1536, 15)
        task_mine = await db.create_task(
            id="test-project/mine-vec", project_id="test-project", goal="Mine"
        )
        task_other = await db.create_task(
            id="other-proj/theirs-vec", project_id="other-proj", goal="Theirs"
        )
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            for task in [task_mine, task_other]:
                rows = await conn.execute_fetchall("SELECT rowid FROM tasks WHERE id = ?", (task["id"],))
                await conn.execute("INSERT OR REPLACE INTO tasks_vec(rowid, embedding) VALUES (?, ?)",
                                   (rows[0]["rowid"], encode_vector(vec)))
            await conn.commit()

        results = await search_tasks_semantic(
            query_vector=vec, project_id="test-project", limit=10
        )
        ids = [r["task_id"] for r in results]
        assert task_mine["id"] in ids
        assert task_other["id"] not in ids

    async def test_empty_when_no_vec0_entries(self, db, sample_project):
        """Returns empty list when tasks_vec is empty."""
        await db.create_task(
            id="test-project/no-vec", project_id="test-project", goal="No vec"
        )
        vec = _unit_vec(1536, 6)
        results = await search_tasks_semantic(query_vector=vec, limit=5)
        assert results == []


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

    async def test_returns_expected_fields(self, db):
        """Result dicts contain required fields."""
        vec = _unit_vec(1536, 9)
        from switchboard.db.connection import get_db
        from switchboard.db._helpers import now_iso
        async with get_db() as conn:
            cursor = await conn.execute(
                "INSERT INTO messages (author, type, content, created_at) VALUES (?, ?, ?, ?)",
                ("tester", "note", "field check", now_iso()),
            )
            await conn.commit()
            msg_id = cursor.lastrowid
            blob = encode_vector(vec)
            cur_c = await conn.execute(
                "INSERT INTO message_chunks (message_id, chunk_index, heading, content, embedding) VALUES (?, ?, ?, ?, ?)",
                (msg_id, 0, "h", "c", blob),
            )
            await conn.commit()
            chunk_id = cur_c.lastrowid
            await conn.execute("INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                               (chunk_id, blob))
            await conn.commit()

        results = await search_message_chunks(query_vector=vec, limit=5)
        assert len(results) >= 1
        r = results[0]
        for field in ("chunk_id", "message_id", "chunk_index", "chunk_heading",
                      "chunk_content", "similarity", "context_chunks"):
            assert field in r

    async def test_empty_when_no_vec0_entries(self, db):
        """Returns empty list when chunks_vec is empty."""
        vec = _unit_vec(1536, 11)
        results = await search_message_chunks(query_vector=vec, limit=5)
        assert results == []


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

    async def test_backfill_is_idempotent(self, db):
        """Running _backfill_vec_tables twice does not raise errors."""
        from switchboard.server.app import _backfill_vec_tables
        await _backfill_vec_tables()
        await _backfill_vec_tables()  # Should not raise
