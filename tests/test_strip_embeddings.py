"""Tests that embedding fields are stripped from all task and message API responses.

The embedding column is an internal-only 6KB binary blob used for semantic
search. It must never appear in responses returned to callers.
"""

import pytest
from ouvrage.db.connection import get_db


async def _set_task_embedding(task_id: str, blob: bytes) -> None:
    """Directly write a fake embedding blob to a task row (bypasses normal API)."""
    async with get_db() as conn:
        await conn.execute(
            "UPDATE tasks SET embedding = ? WHERE id = ?",
            (blob, task_id),
        )
        await conn.commit()


class TestTaskEmbeddingStripped:
    """embedding field is absent from all task DB functions that return task dicts."""

    FAKE_EMBEDDING = b"\x00\x40\xe4\xbb" * 384  # 1536 floats × 4 bytes = 6144 bytes

    async def test_get_task_strips_embedding(self, db, sample_task):
        task_id = sample_task["id"]
        await _set_task_embedding(task_id, self.FAKE_EMBEDDING)

        result = await db.get_task(task_id)
        assert result is not None
        assert "embedding" not in result

    async def test_update_task_strips_embedding(self, db, sample_task):
        task_id = sample_task["id"]
        await _set_task_embedding(task_id, self.FAKE_EMBEDDING)

        result = await db.update_task(task_id, phase="implementing")
        assert "embedding" not in result

    async def test_get_task_status_strips_embedding(self, db, sample_task):
        task_id = sample_task["id"]
        await _set_task_embedding(task_id, self.FAKE_EMBEDDING)

        result = await db.get_task_status(task_id)
        assert "embedding" not in result

    async def test_list_tasks_strips_embedding(self, db, sample_task):
        task_id = sample_task["id"]
        await _set_task_embedding(task_id, self.FAKE_EMBEDDING)

        tasks = await db.list_tasks(project_id="test-project", active_only=False)
        assert len(tasks) >= 1
        for t in tasks:
            assert "embedding" not in t, f"embedding found in task {t['id']}"

    async def test_get_chain_strips_embedding(self, db, completed_chain):
        for task in completed_chain.values():
            await _set_task_embedding(task["id"], self.FAKE_EMBEDDING)

        chain = await db.get_chain("test-project/chain-a")
        assert len(chain) >= 1
        for t in chain:
            assert "embedding" not in t, f"embedding found in chain task {t['id']}"

    async def test_get_dependents_strips_embedding(self, db, completed_chain):
        await _set_task_embedding(completed_chain["b"]["id"], self.FAKE_EMBEDDING)
        await _set_task_embedding(completed_chain["c"]["id"], self.FAKE_EMBEDDING)

        dependents = await db.get_dependents("test-project/chain-a")
        for t in dependents:
            assert "embedding" not in t, f"embedding found in dependent task {t['id']}"

    async def test_get_task_status_recent_messages_strip_embedding(self, db, sample_task):
        """Messages inside get_task_status result are also free of embedding."""
        task_id = sample_task["id"]
        await db.post_task_message(
            task_id=task_id,
            author="cc-worker",
            content="some progress",
            type="progress",
        )
        # Inject a fake embedding into the message row
        async with get_db() as conn:
            await conn.execute("UPDATE messages SET embedding = ? WHERE task_id = ?",
                               (self.FAKE_EMBEDDING, task_id))
            await conn.commit()

        result = await db.get_task_status(task_id)
        for msg in result.get("recent_messages", []):
            assert "embedding" not in msg, "embedding found in recent_messages"
