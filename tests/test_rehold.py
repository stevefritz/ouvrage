"""Tests for re-hold capability: update_task(held=True) on ready tasks."""

import pytest


class TestReHoldTask:
    """update_task(held=True) re-holds ready tasks and rejects all others."""

    async def test_rehold_works_on_ready_task(self, db, sample_project):
        from ouvrage.server.handlers.tasks import _handle_update_task

        task = await db.create_task(
            id="test-project/rehold-ready",
            project_id="test-project",
            goal="Re-hold me",
        )
        # Tasks start as ready
        assert task["status"] == "ready"

        result = await _handle_update_task({"task_id": task["id"], "held": True})
        assert result["held"]
        assert result["status"] == "ready"

    async def test_rehold_works_on_ready_task_with_depends_on(self, db, sample_project):
        """A ready task waiting on a parent can also be re-held."""
        from ouvrage.server.handlers.tasks import _handle_update_task

        parent = await db.create_task(
            id="test-project/rehold-parent",
            project_id="test-project",
            goal="Parent task",
        )
        child = await db.create_task(
            id="test-project/rehold-child",
            project_id="test-project",
            goal="Child task",
            depends_on=parent["id"],
        )
        assert child["status"] == "ready"

        result = await _handle_update_task({"task_id": child["id"], "held": True})
        assert result["held"]
        assert result["status"] == "ready"

    async def test_rehold_returns_full_task_state(self, db, sample_project):
        """update_task returns the full updated task dict."""
        from ouvrage.server.handlers.tasks import _handle_update_task

        task = await db.create_task(
            id="test-project/rehold-return",
            project_id="test-project",
            goal="Check return value",
        )
        result = await _handle_update_task({"task_id": task["id"], "held": True})
        assert "id" in result
        assert result["id"] == task["id"]
        assert result["held"]

    async def test_rehold_persists_to_db(self, db, sample_project):
        """held=True is actually stored in the database."""
        from ouvrage.server.handlers.tasks import _handle_update_task

        task = await db.create_task(
            id="test-project/rehold-persist",
            project_id="test-project",
            goal="Persistence check",
        )
        await _handle_update_task({"task_id": task["id"], "held": True})

        stored = await db.get_task(task["id"])
        assert stored["held"]

    async def test_rehold_errors_on_working_task(self, db, sample_project):
        """Cannot re-hold a working task — suggests cancel_task."""
        from ouvrage.server.handlers.tasks import _handle_update_task

        task = await db.create_task(
            id="test-project/rehold-working",
            project_id="test-project",
            goal="Working task",
        )
        await db.update_task(task["id"], status="working")

        with pytest.raises(ValueError, match="cancel_task"):
            await _handle_update_task({"task_id": task["id"], "held": True})

    async def test_rehold_errors_on_completed_task(self, db, sample_project):
        """Cannot re-hold a completed task — suggests reopen_task."""
        from ouvrage.server.handlers.tasks import _handle_update_task

        task = await db.create_task(
            id="test-project/rehold-completed",
            project_id="test-project",
            goal="Completed task",
        )
        await db.update_task(task["id"], status="completed")

        with pytest.raises(ValueError, match="reopen_task"):
            await _handle_update_task({"task_id": task["id"], "held": True})

    async def test_rehold_errors_on_cancelled_task(self, db, sample_project):
        """Cannot re-hold a cancelled task."""
        from ouvrage.server.handlers.tasks import _handle_update_task

        task = await db.create_task(
            id="test-project/rehold-cancelled",
            project_id="test-project",
            goal="Cancelled task",
        )
        await db.update_task(task["id"], status="cancelled")

        with pytest.raises(ValueError, match="cancelled"):
            await _handle_update_task({"task_id": task["id"], "held": True})

    async def test_rehold_errors_on_failed_task(self, db, sample_project):
        """Cannot re-hold a failed task — only ready tasks allowed."""
        from ouvrage.server.handlers.tasks import _handle_update_task

        task = await db.create_task(
            id="test-project/rehold-failed",
            project_id="test-project",
            goal="Failed task",
        )
        await db.update_task(task["id"], status="failed")

        with pytest.raises(ValueError, match="ready"):
            await _handle_update_task({"task_id": task["id"], "held": True})

    async def test_set_held_false_on_ready_task_has_no_restriction(self, db, sample_project):
        """held=False (clearing hold) on a ready task requires no validation."""
        from ouvrage.server.handlers.tasks import _handle_update_task

        task = await db.create_task(
            id="test-project/rehold-clear",
            project_id="test-project",
            goal="Clear hold",
        )
        await db.update_task(task["id"], held=True)
        result = await _handle_update_task({"task_id": task["id"], "held": False})
        assert not result["held"]

    async def test_rehold_already_held_task_is_idempotent(self, db, sample_project):
        """Re-holding an already-held ready task succeeds without error."""
        from ouvrage.server.handlers.tasks import _handle_update_task

        task = await db.create_task(
            id="test-project/rehold-idempotent",
            project_id="test-project",
            goal="Idempotent re-hold",
        )
        await db.update_task(task["id"], held=True)
        result = await _handle_update_task({"task_id": task["id"], "held": True})
        assert result["held"]
        assert result["status"] == "ready"
