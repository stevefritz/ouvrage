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


# ---------------------------------------------------------------------------
# set_task_embedding / get_tasks_needing_embedding
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# search_tasks_semantic
# ---------------------------------------------------------------------------

def _unit_vector(dim: int, index: int) -> list[float]:
    """Create a unit vector with 1.0 at the given index."""
    v = [0.0] * dim
    v[index % dim] = 1.0
    return v


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


