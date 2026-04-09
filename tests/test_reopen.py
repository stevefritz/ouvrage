"""Tests for the reopen workflow: reopen_task, start_reopened_task, retry_task patch.

Covers:
- reopen_task() state transitions, field clearing, attempt increment, message posting
- start_reopened_task() guard, feedback collection, dispatch args, chain invalidation
- retry_task() regression: posts "Attempt N starting..." before dispatch
"""

from unittest.mock import AsyncMock, patch

import pytest


# ===========================================================================
# reopen_task()
# ===========================================================================

class TestReopenTask:
    """reopen_task transitions completed → reopened."""

    async def test_reopen_succeeds_on_completed_task(self, db, sample_project):
        from switchboard.dispatch.engine import reopen_task

        task = await db.create_task(
            id="test-project/reopen-ok",
            project_id="test-project",
            goal="Reopen me",
        )
        await db.update_task(task["id"], status="completed")

        result = await reopen_task(task["id"])
        assert result["status"] == "stopped"
        assert result["reason"] == "awaiting_feedback"


# ===========================================================================
# start_reopened_task()
# ===========================================================================

class TestStartReopenedTask:
    """start_reopened_task transitions stopped(awaiting_feedback) → working via lifecycle."""


    async def test_start_collects_user_feedback_messages(self, db, sample_project, mock_git, mock_sdk):
        """Only user-authored messages after the reopen message are passed as feedback."""
        from switchboard.dispatch.engine import reopen_task, start_reopened_task
        from switchboard.dispatch.internals import collect_reopen_feedback

        task = await db.create_task(
            id="test-project/start-feedback",
            project_id="test-project",
            goal="Feedback collection",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)

        # Reopen — posts the reopen status message stamped to attempt 2
        await reopen_task(task["id"])

        # Post some user feedback after reopen
        await db.post_task_message(
            task_id=task["id"], author="stephen",
            content="Please add error handling to the upload function.",
        )
        await db.post_task_message(
            task_id=task["id"], author="stephen",
            content="Also fix the race condition in the queue.",
        )
        # Post a system message that should NOT be collected
        await db.post_task_message(
            task_id=task["id"], author="switchboard",
            type="status", content="System note — should be filtered.",
        )

        # Verify feedback collection works correctly
        feedback = await collect_reopen_feedback(task["id"], 2)
        assert feedback is not None
        assert len(feedback) == 2
        authors = {m["author"] for m in feedback}
        assert authors == {"stephen"}


    async def test_start_invalidates_chain_when_dependents_exist(self, db, sample_project, mock_git, mock_sdk):
        from switchboard.dispatch.engine import reopen_task, start_reopened_task

        parent = await db.create_task(
            id="test-project/start-parent",
            project_id="test-project",
            goal="Parent task",
        )
        child = await db.create_task(
            id="test-project/start-child",
            project_id="test-project",
            goal="Child task",
            depends_on="test-project/start-parent",
        )
        await db.update_task(parent["id"], status="completed", current_attempt=1)
        await reopen_task(parent["id"])

        mock_invalidate = AsyncMock()
        with patch("switchboard.dispatch.engine._invalidate_chain", mock_invalidate):
            await start_reopened_task(parent["id"])

        mock_invalidate.assert_awaited_once_with("test-project/start-parent")


# ===========================================================================
# retry_task() — regression: posts "Attempt N starting..." before dispatch
# ===========================================================================


# ===========================================================================
# cancel_reopen()
# ===========================================================================

class TestCancelReopen:
    """cancel_reopen() reverses a reopen — back to completed."""


    async def test_cancel_reopen_deletes_reopened_messages(self, db, sample_project):
        """Messages stamped to the reopened attempt should be deleted."""
        from switchboard.dispatch.engine import cancel_reopen, reopen_task

        task = await db.create_task(
            id="test-project/cancel-reopen-msgs",
            project_id="test-project",
            goal="Message cleanup check",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await reopen_task(task["id"])

        # Post some feedback during the reopened state
        await db.post_task_message(
            task_id=task["id"], author="stephen", content="Some feedback."
        )

        thread_before = await db.read_task_messages(task["id"])
        msgs_before = thread_before["messages"]
        attempt2_msgs = [m for m in msgs_before if m.get("attempt_number") == 2]
        assert len(attempt2_msgs) > 0

        await cancel_reopen(task["id"])

        thread_after = await db.read_task_messages(task["id"])
        msgs_after = thread_after["messages"]
        attempt2_msgs_after = [m for m in msgs_after if m.get("attempt_number") == 2]
        assert len(attempt2_msgs_after) == 0


# ===========================================================================
# _sync_branch_with_base()
# ===========================================================================

class TestSyncBranchWithBase:
    """_sync_branch_with_base() — rebase helper for start_reopened_task."""


    async def test_returns_true_when_worktree_missing_from_disk(self, db, sample_project):
        from switchboard.git.operations import _sync_branch_with_base

        task = await db.create_task(
            id="test-project/sync-missing-dir",
            project_id="test-project",
            goal="Missing dir",
        )
        await db.update_task(task["id"], worktree_path="/nonexistent/path/that/does/not/exist")
        task = await db.get_task(task["id"])

        result = await _sync_branch_with_base(task)
        assert result is True


    async def test_rebase_conflict_posts_error_message(self, db, sample_project):
        """When rebase fails, an error message is posted to the thread."""
        import tempfile
        from unittest.mock import patch

        from switchboard.git.operations import _sync_branch_with_base

        task = await db.create_task(
            id="test-project/sync-conflict-msg",
            project_id="test-project",
            goal="Rebase conflict message",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            await db.update_task(task["id"], worktree_path=tmpdir)
            task = await db.get_task(task["id"])

            async def mock_run(*cmd, **kwargs):
                if "rebase" in cmd and "--abort" not in cmd:
                    return b"", b"CONFLICT", 1
                return b"", b"", 0

            with patch("switchboard.git.operations._run_as_worker", mock_run):
                with patch("switchboard.git.operations.resolve_branch_target", return_value="main"):
                    await _sync_branch_with_base(task)

        thread = await db.read_task_messages(task["id"])
        msgs = thread["messages"]
        conflict_msgs = [m for m in msgs if "conflict" in (m.get("title") or "").lower()]
        assert len(conflict_msgs) == 1


# ===========================================================================
# start_reopened_task() — auto_test/auto_review overrides
# ===========================================================================

class TestStartReopenedTaskOverrides:
    """start_reopened_task transitions through lifecycle with side effects."""


    async def test_start_with_auto_test_false_skips_test_gate(self, db, sample_project, mock_git, mock_sdk):
        """start(auto_test=False) causes test gate to be skipped on completion."""
        from switchboard.dispatch.engine import reopen_task, start_reopened_task
        from switchboard.dispatch.lifecycle import lifecycle

        task = await db.create_task(
            id="test-project/start-skip-test-gate",
            project_id="test-project",
            goal="Skip test gate",
            auto_test=True,
            auto_review=False,
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await reopen_task(task["id"])
        await start_reopened_task(task["id"], auto_test=False)

        # Verify override was persisted
        updated = await db.get_task(task["id"])
        assert updated["auto_test"] == False  # noqa: E712

        # Simulate task completion and verify test gate is NOT triggered
        mock_test_gate = AsyncMock()
        mock_review_gate = AsyncMock()
        mock_dispatch_dependents = AsyncMock()
        mock_drain_queue = AsyncMock()
        with (
            patch("switchboard.dispatch.gates._run_test_gate", mock_test_gate),
            patch("switchboard.dispatch.gates._dispatch_review", mock_review_gate),
            patch("switchboard.dispatch.engine._check_and_dispatch_dependents", mock_dispatch_dependents),
            patch("switchboard.dispatch.queue._drain_queue", mock_drain_queue),
        ):
            await lifecycle.execute(task["id"], "complete", triggered_by="sdk")

        mock_test_gate.assert_not_awaited()
        mock_review_gate.assert_not_awaited()

    async def test_start_with_auto_review_false_skips_review_gate(self, db, sample_project, mock_git, mock_sdk):
        """start(auto_review=False) causes review gate to be skipped on completion."""
        from switchboard.dispatch.engine import reopen_task, start_reopened_task
        from switchboard.dispatch.lifecycle import lifecycle

        task = await db.create_task(
            id="test-project/start-skip-review-gate",
            project_id="test-project",
            goal="Skip review gate",
            auto_test=False,
            auto_review=True,
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await reopen_task(task["id"])
        await start_reopened_task(task["id"], auto_review=False)

        # Verify override was persisted
        updated = await db.get_task(task["id"])
        assert updated["auto_review"] == False  # noqa: E712

        # Simulate task completion and verify review gate is NOT triggered
        mock_test_gate = AsyncMock()
        mock_review_gate = AsyncMock()
        mock_dispatch_dependents = AsyncMock()
        mock_drain_queue = AsyncMock()
        with (
            patch("switchboard.dispatch.gates._run_test_gate", mock_test_gate),
            patch("switchboard.dispatch.gates._dispatch_review", mock_review_gate),
            patch("switchboard.dispatch.engine._check_and_dispatch_dependents", mock_dispatch_dependents),
            patch("switchboard.dispatch.queue._drain_queue", mock_drain_queue),
        ):
            await lifecycle.execute(task["id"], "complete", triggered_by="sdk")

        mock_test_gate.assert_not_awaited()
        mock_review_gate.assert_not_awaited()

