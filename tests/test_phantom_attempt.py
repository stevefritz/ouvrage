"""Tests for the phantom attempt bug fix.

Scenario:
  1. Task completes, auto-test gate runs
  2. Tests fail → gate calls retry_task
  3. retry_task increments current_attempt, posts "Attempt N starting"
  4. dispatch_task raises (project paused, worktree error, etc.)
  5. BUG: phantom attempt N exists with no running worker
  6. FIX: task is set to needs-review, clear error message posted
"""

from unittest.mock import AsyncMock, patch

import pytest

from switchboard.dispatch.engine import retry_task


class TestPhantomAttemptBugFix:
    """retry_task rolls back gracefully when dispatch_task raises."""

    @pytest.fixture(autouse=True)
    def _base_patches(self):
        """Suppress log archiving and punchlist operations (filesystem/DB heavy)."""
        patches = [
            patch("switchboard.dispatch.engine.archive_task_logs", AsyncMock()),
            patch("switchboard.dispatch.engine.db.revert_punchlist_items_for_task", AsyncMock(return_value=0)),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_dispatch_failure_sets_needs_review(self, db, sample_project):
        """When dispatch_task raises, task status becomes needs-review."""
        task = await db.create_task(
            id="test-project/phantom-task",
            project_id="test-project",
            goal="Fix the thing",
        )
        await db.update_task(task["id"], status="completed")

        with patch("switchboard.dispatch.engine.dispatch_task",
                   AsyncMock(side_effect=ValueError("Project 'test-project' is paused."))):
            result = await retry_task("test-project/phantom-task")

        stored = await db.get_task("test-project/phantom-task")
        assert stored["status"] == "needs-review"
        assert result["status"] == "needs-review"
        assert "error" in result

    async def test_dispatch_failure_increments_attempt(self, db, sample_project):
        """current_attempt is still incremented even on dispatch failure (attempt started)."""
        task = await db.create_task(
            id="test-project/phantom-attempt",
            project_id="test-project",
            goal="Fix the thing",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)

        with patch("switchboard.dispatch.engine.dispatch_task",
                   AsyncMock(side_effect=RuntimeError("worktree setup failed"))):
            await retry_task("test-project/phantom-attempt")

        stored = await db.get_task("test-project/phantom-attempt")
        assert stored["current_attempt"] == 2

    async def test_dispatch_failure_posts_error_message(self, db, sample_project):
        """A clear 'Auto-retry dispatch failed' message is posted."""
        task = await db.create_task(
            id="test-project/phantom-msg",
            project_id="test-project",
            goal="Fix the thing",
        )
        await db.update_task(task["id"], status="completed")

        error_text = "Project 'test-project' is paused."
        with patch("switchboard.dispatch.engine.dispatch_task",
                   AsyncMock(side_effect=ValueError(error_text))):
            await retry_task("test-project/phantom-msg")

        thread = await db.read_task_messages("test-project/phantom-msg")
        messages = thread.get("messages", [])

        # Should have: "Attempt N starting" + "Auto-retry dispatch failed"
        titles = [m.get("title") for m in messages]
        assert any("Auto-retry dispatch failed" in (t or "") for t in titles)

        # The error message should contain the reason
        failure_msg = next(
            (m for m in messages if "Auto-retry dispatch failed" in (m.get("title") or "")),
            None,
        )
        assert failure_msg is not None
        assert error_text in failure_msg["content"]

    async def test_dispatch_failure_does_not_leave_working_status(self, db, sample_project):
        """Task must NOT be in 'working' status with no running worker."""
        task = await db.create_task(
            id="test-project/no-ghost-working",
            project_id="test-project",
            goal="Fix the thing",
        )
        await db.update_task(task["id"], status="completed")

        with patch("switchboard.dispatch.engine.dispatch_task",
                   AsyncMock(side_effect=Exception("some dispatch error"))):
            await retry_task("test-project/no-ghost-working")

        stored = await db.get_task("test-project/no-ghost-working")
        assert stored["status"] != "working", (
            f"Task should not show as 'working' with no worker — got: {stored['status']}"
        )

    async def test_successful_retry_still_works(self, db, sample_project):
        """Normal successful retry path is not broken."""
        task = await db.create_task(
            id="test-project/success-retry",
            project_id="test-project",
            goal="Fix the thing",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)

        mock_dispatch_result = {
            "task_id": "test-project/success-retry",
            "status": "working",
            "phase": "analysis",
            "worktree_path": "/tmp/fake",
            "branch": "success-retry",
            "session_id": None,
            "dispatch_count": 2,
            "max_turns": 50,
            "max_wall_clock": 30,
            "model": "sonnet",
            "resumed": False,
            "queued": False,
        }
        with patch("switchboard.dispatch.engine.dispatch_task",
                   AsyncMock(return_value=mock_dispatch_result)):
            result = await retry_task("test-project/success-retry")

        assert result["status"] == "working"
        stored = await db.get_task("test-project/success-retry")
        assert stored["current_attempt"] == 2
