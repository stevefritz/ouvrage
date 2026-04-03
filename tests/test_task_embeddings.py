"""Tests for task goal vector embeddings.

Covers:
- Schema migration: embedding column added to tasks table
- set_task_embedding / get_tasks_needing_embedding DB functions
- search_tasks_semantic cosine similarity search
- Embed-on-dispatch: goal embedded when task is created
- Startup backfill: tasks with NULL embeddings get embedded
- Graceful no-op when OPENAI_API_KEY is not set (embed_safe returns None)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_adds_embedding_column_to_tasks(db):
    """After init_db(), tasks table should have an embedding column."""
    async with db.get_db() as conn:
        cols = await conn.execute_fetchall("PRAGMA table_info(tasks)")
        col_names = [c["name"] for c in cols]
    assert "embedding" in col_names


# ---------------------------------------------------------------------------
# set_task_embedding / get_tasks_needing_embedding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_task_embedding_stores_blob(db, sample_task):
    """set_task_embedding persists a blob that can be read back."""
    from switchboard.embeddings.service import encode_vector, decode_vector

    vector = [float(i) / 1536 for i in range(1536)]
    blob = encode_vector(vector)
    await db.set_task_embedding(sample_task["id"], blob)

    async with db.get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT embedding FROM tasks WHERE id = ?", (sample_task["id"],)
        )
    assert rows[0]["embedding"] is not None
    decoded = decode_vector(rows[0]["embedding"])
    assert len(decoded) == 1536
    for a, b in zip(vector[:10], decoded[:10]):
        assert abs(a - b) < 1e-5


@pytest.mark.asyncio
async def test_get_tasks_needing_embedding_returns_unembedded(db, sample_task):
    """get_tasks_needing_embedding returns tasks with NULL embeddings."""
    rows = await db.get_tasks_needing_embedding(batch_size=100)
    ids = [r["id"] for r in rows]
    assert sample_task["id"] in ids


@pytest.mark.asyncio
async def test_get_tasks_needing_embedding_excludes_embedded(db, sample_task):
    """Tasks that already have an embedding are excluded."""
    from switchboard.embeddings.service import encode_vector

    blob = encode_vector([0.1] * 1536)
    await db.set_task_embedding(sample_task["id"], blob)

    rows = await db.get_tasks_needing_embedding(batch_size=100)
    ids = [r["id"] for r in rows]
    assert sample_task["id"] not in ids


@pytest.mark.asyncio
async def test_get_tasks_needing_embedding_batch_limit(db, sample_project):
    """batch_size parameter limits the number returned."""
    for i in range(5):
        await db.create_task(
            id=f"test-project/batch-task-{i}",
            project_id="test-project",
            goal=f"Batch task {i}",
        )
    rows = await db.get_tasks_needing_embedding(batch_size=3)
    assert len(rows) <= 3


# ---------------------------------------------------------------------------
# search_tasks_semantic
# ---------------------------------------------------------------------------

def _unit_vector(dim: int, index: int) -> list[float]:
    """Create a unit vector with 1.0 at the given index."""
    v = [0.0] * dim
    v[index % dim] = 1.0
    return v


@pytest.mark.asyncio
async def test_search_tasks_semantic_returns_ranked_results(db, sample_project):
    """search_tasks_semantic ranks tasks by cosine similarity."""
    from switchboard.embeddings.service import encode_vector

    task_a = await db.create_task(
        id="test-project/search-a",
        project_id="test-project",
        goal="Implement Docker containerization for the API",
    )
    task_b = await db.create_task(
        id="test-project/search-b",
        project_id="test-project",
        goal="Fix authentication bug in login flow",
    )

    vec_a = _unit_vector(1536, 0)
    vec_b = _unit_vector(1536, 1)
    await db.set_task_embedding(task_a["id"], encode_vector(vec_a))
    await db.set_task_embedding(task_b["id"], encode_vector(vec_b))

    # Query with vec_a — task_a should rank first
    results = await db.search_tasks_semantic(query_vector=vec_a, limit=10)
    ids = [r["task_id"] for r in results]
    assert task_a["id"] in ids
    assert results[0]["task_id"] == task_a["id"]


@pytest.mark.asyncio
async def test_search_tasks_semantic_result_shape(db, sample_project):
    """search_tasks_semantic returns dicts with required fields."""
    from switchboard.embeddings.service import encode_vector

    task = await db.create_task(
        id="test-project/shape-task",
        project_id="test-project",
        goal="Build the widget API layer",
    )
    vec = _unit_vector(1536, 5)
    await db.set_task_embedding(task["id"], encode_vector(vec))

    results = await db.search_tasks_semantic(query_vector=vec, limit=5)
    assert len(results) >= 1
    r = results[0]
    assert "task_id" in r
    assert "project_id" in r
    assert "goal" in r
    assert "status" in r
    assert "similarity" in r
    assert 0.0 <= r["similarity"] <= 1.0


@pytest.mark.asyncio
async def test_search_tasks_semantic_filters_by_project(db, sample_project):
    """project_id filter scopes results to that project."""
    from switchboard.embeddings.service import encode_vector

    await db.create_project(
        id="other-project", repo="https://github.com/x/y.git",
        working_dir="/work/y", default_branch="main",
    )

    task_mine = await db.create_task(
        id="test-project/mine",
        project_id="test-project",
        goal="Task in test-project",
    )
    task_other = await db.create_task(
        id="other-project/theirs",
        project_id="other-project",
        goal="Task in other-project",
    )

    vec = _unit_vector(1536, 10)
    blob = encode_vector(vec)
    await db.set_task_embedding(task_mine["id"], blob)
    await db.set_task_embedding(task_other["id"], blob)

    results = await db.search_tasks_semantic(
        query_vector=vec, project_id="test-project", limit=10
    )
    ids = [r["task_id"] for r in results]
    assert task_mine["id"] in ids
    assert task_other["id"] not in ids


@pytest.mark.asyncio
async def test_search_tasks_semantic_empty_when_no_embeddings(db, sample_project):
    """Returns empty list when no tasks have embeddings."""
    await db.create_task(
        id="test-project/no-embed",
        project_id="test-project",
        goal="Task with no embedding",
    )
    vec = _unit_vector(1536, 3)
    results = await db.search_tasks_semantic(query_vector=vec, limit=5)
    assert results == []


# ---------------------------------------------------------------------------
# Embed on dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goal_embedded_on_dispatch(db, sample_project, mock_git, mock_sdk):
    """Dispatching a task fires the goal embedding asynchronously."""
    from switchboard.embeddings.service import set_embedding_service, EmbeddingService

    embed_calls = []

    class TrackingService(EmbeddingService):
        async def embed(self, text):
            embed_calls.append(text)
            return [0.5] * 1536

    set_embedding_service(TrackingService())

    try:
        from switchboard.dispatch import engine as task_engine
        result = await task_engine.dispatch_task(
            project_id="test-project",
            task_id="test-project/embed-dispatch-test",
            goal="Implement Docker containerization for the API",
            held=True,  # don't actually launch CC
        )

        # Allow the async task to complete
        await asyncio.sleep(0.1)

        # Goal should have been embedded
        assert len(embed_calls) == 1
        assert embed_calls[0] == "Implement Docker containerization for the API"

        # Embedding should be stored in DB
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT embedding FROM tasks WHERE id = ?",
                ("test-project/embed-dispatch-test",),
            )
        assert rows[0]["embedding"] is not None
    finally:
        set_embedding_service(None)


@pytest.mark.asyncio
async def test_short_goal_is_embedded(db, sample_project, mock_git, mock_sdk):
    """Short goals (under 50 chars) are still embedded — no minimum length for goals."""
    from switchboard.embeddings.service import set_embedding_service, EmbeddingService

    embed_calls = []

    class TrackingService(EmbeddingService):
        async def embed(self, text):
            embed_calls.append(text)
            return [0.5] * 1536

    set_embedding_service(TrackingService())

    try:
        from switchboard.dispatch import engine as task_engine
        await task_engine.dispatch_task(
            project_id="test-project",
            task_id="test-project/short-goal-test",
            goal="Fix auth bug",  # 12 chars — well under 50
            held=True,
        )

        await asyncio.sleep(0.1)

        # Should still be embedded despite being short
        assert len(embed_calls) == 1
        assert embed_calls[0] == "Fix auth bug"
    finally:
        set_embedding_service(None)


@pytest.mark.asyncio
async def test_embed_no_op_when_openai_key_missing(db, sample_project, mock_git, mock_sdk):
    """When OPENAI_API_KEY is not set, embed_safe returns None gracefully — no error."""
    from switchboard.embeddings.service import set_embedding_service, EmbeddingService

    class NoKeyService(EmbeddingService):
        async def embed(self, text):
            raise ValueError("OPENAI_API_KEY environment variable not set.")

        async def embed_safe(self, text):
            # Simulates what OpenAIEmbeddingService does when key is missing
            return None

    set_embedding_service(NoKeyService())

    try:
        from switchboard.dispatch import engine as task_engine
        # Should not raise
        await task_engine.dispatch_task(
            project_id="test-project",
            task_id="test-project/no-key-test",
            goal="Task that won't get embedded",
            held=True,
        )

        await asyncio.sleep(0.1)

        # Embedding should be NULL — no error raised
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT embedding FROM tasks WHERE id = ?",
                ("test-project/no-key-test",),
            )
        assert rows[0]["embedding"] is None
    finally:
        set_embedding_service(None)


# ---------------------------------------------------------------------------
# Startup backfill
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_task_goals_embeds_unembedded_tasks(db, sample_project):
    """_backfill_task_goals processes tasks with NULL embeddings."""
    from switchboard.embeddings.service import set_embedding_service, EmbeddingService
    from switchboard.server.app import _backfill_task_goals

    task_a = await db.create_task(
        id="test-project/backfill-a",
        project_id="test-project",
        goal="First task to be backfilled",
    )
    task_b = await db.create_task(
        id="test-project/backfill-b",
        project_id="test-project",
        goal="Second task to be backfilled",
    )

    class FakeService(EmbeddingService):
        async def embed(self, text):
            return [0.25] * 1536

    set_embedding_service(FakeService())

    try:
        await _backfill_task_goals()

        # Both tasks should now have embeddings
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT id, embedding FROM tasks WHERE id IN (?, ?)",
                (task_a["id"], task_b["id"]),
            )
        for row in rows:
            assert row["embedding"] is not None, f"Task {row['id']} has no embedding after backfill"
    finally:
        set_embedding_service(None)


@pytest.mark.asyncio
async def test_backfill_task_goals_skips_already_embedded(db, sample_project):
    """_backfill_task_goals skips tasks that already have an embedding."""
    from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector
    from switchboard.server.app import _backfill_task_goals

    task = await db.create_task(
        id="test-project/already-embedded",
        project_id="test-project",
        goal="This task was already embedded",
    )
    existing_blob = encode_vector([0.9] * 1536)
    await db.set_task_embedding(task["id"], existing_blob)

    embed_calls = []

    class TrackingService(EmbeddingService):
        async def embed(self, text):
            embed_calls.append(text)
            return [0.5] * 1536

    set_embedding_service(TrackingService())

    try:
        await _backfill_task_goals()

        # The already-embedded task should not have been re-embedded
        # (embed was not called for its goal)
        assert "This task was already embedded" not in embed_calls
    finally:
        set_embedding_service(None)


@pytest.mark.asyncio
async def test_backfill_task_goals_no_op_when_embed_fails(db, sample_project):
    """_backfill_task_goals continues processing when embedding fails for a task."""
    from switchboard.embeddings.service import set_embedding_service, EmbeddingService
    from switchboard.server.app import _backfill_task_goals

    task_a = await db.create_task(
        id="test-project/fail-task",
        project_id="test-project",
        goal="Task that will fail to embed",
    )
    task_b = await db.create_task(
        id="test-project/ok-task",
        project_id="test-project",
        goal="Task that will embed successfully",
    )

    call_count = [0]

    class PartialFailService(EmbeddingService):
        async def embed(self, text):
            call_count[0] += 1
            if "fail" in text:
                raise RuntimeError("Simulated embedding failure")
            return [0.5] * 1536

        async def embed_safe(self, text):
            try:
                return await self.embed(text)
            except Exception:
                return None

    set_embedding_service(PartialFailService())

    try:
        # Should not raise even though one task fails
        await _backfill_task_goals()

        # The ok task should be embedded
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT id, embedding FROM tasks WHERE id = ?", (task_b["id"],)
            )
        assert rows[0]["embedding"] is not None
    finally:
        set_embedding_service(None)
