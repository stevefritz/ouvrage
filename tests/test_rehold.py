"""Tests for re-hold capability: update_task(held=True) on ready tasks."""

import pytest


class TestReHoldTask:
    """update_task(held=True) re-holds ready tasks and rejects all others."""

    async def test_rehold_works_on_ready_task(self, db, sample_project):
        from switchboard.server.handlers.tasks import _handle_update_task

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


    async def test_rehold_errors_on_working_task(self, db, sample_project):
        """Cannot re-hold a working task — suggests cancel_task."""
        from switchboard.server.handlers.tasks import _handle_update_task

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
        from switchboard.server.handlers.tasks import _handle_update_task

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
        from switchboard.server.handlers.tasks import _handle_update_task

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
        from switchboard.server.handlers.tasks import _handle_update_task

        task = await db.create_task(
            id="test-project/rehold-failed",
            project_id="test-project",
            goal="Failed task",
        )
        await db.update_task(task["id"], status="failed")

        with pytest.raises(ValueError, match="ready"):
            await _handle_update_task({"task_id": task["id"], "held": True})


