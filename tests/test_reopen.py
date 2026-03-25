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
        import tasks

        task = await db.create_task(
            id="test-project/reopen-ok",
            project_id="test-project",
            goal="Reopen me",
        )
        await db.update_task(task["id"], status="completed")

        result = await tasks.reopen_task(task["id"])
        assert result["status"] == "reopened"

    async def test_reopen_fails_on_non_completed_task(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/reopen-fail-working",
            project_id="test-project",
            goal="Not completed",
        )
        await db.update_task(task["id"], status="working")

        with pytest.raises(ValueError, match="must be 'completed'"):
            await tasks.reopen_task(task["id"])

    async def test_reopen_fails_on_failed_task(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/reopen-fail-failed",
            project_id="test-project",
            goal="Failed task",
        )
        await db.update_task(task["id"], status="failed")

        with pytest.raises(ValueError, match="must be 'completed'"):
            await tasks.reopen_task(task["id"])

    async def test_reopen_fails_on_missing_task(self, db, sample_project):
        import tasks

        with pytest.raises(ValueError, match="not found"):
            await tasks.reopen_task("test-project/does-not-exist")

    async def test_reopen_increments_current_attempt(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/reopen-attempt",
            project_id="test-project",
            goal="Increment attempt",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)

        await tasks.reopen_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["current_attempt"] == 2

    async def test_reopen_increments_from_higher_attempt(self, db, sample_project):
        """Works correctly when current_attempt > 1."""
        import tasks

        task = await db.create_task(
            id="test-project/reopen-attempt-3",
            project_id="test-project",
            goal="Third attempt reopen",
        )
        await db.update_task(task["id"], status="completed", current_attempt=3)

        await tasks.reopen_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["current_attempt"] == 4

    async def test_reopen_sets_status_to_reopened(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/reopen-status",
            project_id="test-project",
            goal="Status check",
        )
        await db.update_task(task["id"], status="completed")

        await tasks.reopen_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["status"] == "reopened"

    async def test_reopen_clears_session_id(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/reopen-clear-session",
            project_id="test-project",
            goal="Clear session",
        )
        await db.update_task(task["id"], status="completed", session_id="ses_abc123")

        await tasks.reopen_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["session_id"] is None

    async def test_reopen_clears_gate_status(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/reopen-clear-gate",
            project_id="test-project",
            goal="Clear gate",
        )
        await db.update_task(
            task["id"],
            status="completed",
            gate_status="passed",
            gate_passed_at=db.now_iso(),
        )

        await tasks.reopen_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["gate_status"] is None
        assert updated["gate_passed_at"] is None

    async def test_reopen_posts_status_message_stamped_to_new_attempt(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/reopen-msg",
            project_id="test-project",
            goal="Message stamp check",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)

        await tasks.reopen_task(task["id"])

        thread = await db.read_task_messages(task["id"])
        msgs = thread["messages"]
        reopen_msgs = [m for m in msgs if "reopened" in (m.get("title") or "").lower()]
        assert len(reopen_msgs) == 1
        # Message must be stamped to the new attempt (2)
        assert reopen_msgs[0]["attempt_number"] == 2

    async def test_reopen_posts_awaiting_feedback_message(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/reopen-msg-content",
            project_id="test-project",
            goal="Message content check",
        )
        await db.update_task(task["id"], status="completed")

        await tasks.reopen_task(task["id"])

        thread = await db.read_task_messages(task["id"])
        msgs = thread["messages"]
        reopen_msgs = [m for m in msgs if m.get("author") == "switchboard"]
        assert len(reopen_msgs) == 1
        assert "awaiting feedback" in reopen_msgs[0]["title"].lower()
        assert reopen_msgs[0]["type"] == "status"


# ===========================================================================
# start_reopened_task()
# ===========================================================================

class TestStartReopenedTask:
    """start_reopened_task transitions reopened → working via dispatch."""

    async def test_start_fails_on_non_reopened_task(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/start-fail-completed",
            project_id="test-project",
            goal="Not reopened",
        )
        await db.update_task(task["id"], status="completed")

        with patch("tasks.dispatch_task", AsyncMock(return_value={"status": "working"})):
            with pytest.raises(ValueError, match="must be 'reopened'"):
                await tasks.start_reopened_task(task["id"])

    async def test_start_fails_on_missing_task(self, db, sample_project):
        import tasks

        with patch("tasks.dispatch_task", AsyncMock(return_value={"status": "working"})):
            with pytest.raises(ValueError, match="not found"):
                await tasks.start_reopened_task("test-project/no-such-task")

    async def test_start_calls_dispatch_with_phase_revisions(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/start-phase",
            project_id="test-project",
            goal="Phase check",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await tasks.reopen_task(task["id"])

        mock_dispatch = AsyncMock(return_value={"status": "working"})
        with patch("tasks.dispatch_task", mock_dispatch):
            with patch("tasks._invalidate_chain", AsyncMock()):
                await tasks.start_reopened_task(task["id"])

        mock_dispatch.assert_awaited_once()
        call_kwargs = mock_dispatch.await_args.kwargs
        assert call_kwargs["phase"] == "revisions"

    async def test_start_collects_user_feedback_messages(self, db, sample_project):
        """Only user-authored messages after the reopen message are passed as feedback."""
        import tasks

        task = await db.create_task(
            id="test-project/start-feedback",
            project_id="test-project",
            goal="Feedback collection",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)

        # Reopen — posts the reopen status message stamped to attempt 2
        await tasks.reopen_task(task["id"])

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

        mock_dispatch = AsyncMock(return_value={"status": "working"})
        with patch("tasks.dispatch_task", mock_dispatch):
            with patch("tasks._invalidate_chain", AsyncMock()):
                await tasks.start_reopened_task(task["id"])

        call_kwargs = mock_dispatch.await_args.kwargs
        feedback = call_kwargs.get("review_feedback")
        assert feedback is not None
        assert len(feedback) == 2
        authors = {m["author"] for m in feedback}
        assert authors == {"stephen"}

    async def test_start_excludes_system_authors_from_feedback(self, db, sample_project):
        """switchboard, dispatcher, cc-worker messages are not included in feedback."""
        import tasks

        task = await db.create_task(
            id="test-project/start-filter",
            project_id="test-project",
            goal="Author filter check",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await tasks.reopen_task(task["id"])

        # Post messages from system authors — should all be filtered
        await db.post_task_message(
            task_id=task["id"], author="dispatcher", content="System dispatch msg."
        )
        await db.post_task_message(
            task_id=task["id"], author="cc-worker", type="result", content="CC result msg."
        )

        mock_dispatch = AsyncMock(return_value={"status": "working"})
        with patch("tasks.dispatch_task", mock_dispatch):
            with patch("tasks._invalidate_chain", AsyncMock()):
                await tasks.start_reopened_task(task["id"])

        call_kwargs = mock_dispatch.await_args.kwargs
        # No user feedback — review_feedback should be None
        assert call_kwargs.get("review_feedback") is None

    async def test_start_passes_correct_task_id_and_goal(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/start-args",
            project_id="test-project",
            goal="My specific goal",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await tasks.reopen_task(task["id"])

        mock_dispatch = AsyncMock(return_value={"status": "working"})
        with patch("tasks.dispatch_task", mock_dispatch):
            with patch("tasks._invalidate_chain", AsyncMock()):
                await tasks.start_reopened_task(task["id"])

        call_kwargs = mock_dispatch.await_args.kwargs
        assert call_kwargs["task_id"] == "test-project/start-args"
        assert call_kwargs["goal"] == "My specific goal"

    async def test_start_posts_attempt_starting_message(self, db, sample_project):
        """start_reopened_task posts 'Attempt N starting' before dispatching."""
        import tasks

        task = await db.create_task(
            id="test-project/start-msg",
            project_id="test-project",
            goal="Starting message check",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await tasks.reopen_task(task["id"])

        mock_dispatch = AsyncMock(return_value={"status": "working"})
        with patch("tasks.dispatch_task", mock_dispatch):
            with patch("tasks._invalidate_chain", AsyncMock()):
                await tasks.start_reopened_task(task["id"])

        thread = await db.read_task_messages(task["id"])
        msgs = thread["messages"]
        starting_msgs = [m for m in msgs if "starting" in (m.get("title") or "").lower()]
        assert len(starting_msgs) == 1
        assert starting_msgs[0]["attempt_number"] == 2

    async def test_start_invalidates_chain_when_dependents_exist(self, db, sample_project):
        import tasks

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
        await tasks.reopen_task(parent["id"])

        mock_dispatch = AsyncMock(return_value={"status": "working"})
        mock_invalidate = AsyncMock()
        with patch("tasks.dispatch_task", mock_dispatch):
            with patch("tasks._invalidate_chain", mock_invalidate):
                await tasks.start_reopened_task(parent["id"])

        mock_invalidate.assert_awaited_once_with("test-project/start-parent")

    async def test_start_does_not_invalidate_when_no_dependents(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/start-no-deps",
            project_id="test-project",
            goal="No dependents",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await tasks.reopen_task(task["id"])

        mock_dispatch = AsyncMock(return_value={"status": "working"})
        mock_invalidate = AsyncMock()
        with patch("tasks.dispatch_task", mock_dispatch):
            with patch("tasks._invalidate_chain", mock_invalidate):
                await tasks.start_reopened_task(task["id"])

        mock_invalidate.assert_not_awaited()


# ===========================================================================
# retry_task() — regression: posts "Attempt N starting..." before dispatch
# ===========================================================================

class TestRetryTaskStartingMessage:
    """Regression: retry_task posts 'Attempt N starting' message before dispatch."""

    async def test_retry_posts_starting_message(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/retry-starting-msg",
            project_id="test-project",
            goal="Retry starting message regression",
        )
        await db.update_task(task["id"], status="failed", current_attempt=1)

        with patch("tasks.dispatch_task", AsyncMock(return_value={"status": "working"})):
            with patch("tasks._invalidate_chain", AsyncMock()):
                await tasks.retry_task(task["id"])

        thread = await db.read_task_messages(task["id"])
        msgs = thread["messages"]
        starting_msgs = [m for m in msgs if "starting" in (m.get("title") or "").lower()]
        assert len(starting_msgs) == 1
        assert starting_msgs[0]["attempt_number"] == 2
        assert starting_msgs[0]["author"] == "switchboard"

    async def test_retry_starting_message_posted_before_dispatch(self, db, sample_project):
        """The 'Attempt N starting' message must exist in DB before dispatch_task is called."""
        import tasks

        task = await db.create_task(
            id="test-project/retry-order",
            project_id="test-project",
            goal="Message ordering check",
        )
        await db.update_task(task["id"], status="failed", current_attempt=1)

        messages_at_dispatch_time = []

        async def capture_dispatch(**kwargs):
            thread = await db.read_task_messages(task["id"])
            messages_at_dispatch_time.extend(thread["messages"])
            return {"status": "working"}

        with patch("tasks.dispatch_task", AsyncMock(side_effect=capture_dispatch)):
            with patch("tasks._invalidate_chain", AsyncMock()):
                await tasks.retry_task(task["id"])

        starting_msgs = [
            m for m in messages_at_dispatch_time
            if "starting" in (m.get("title") or "").lower()
        ]
        assert len(starting_msgs) == 1, "Starting message must be posted before dispatch_task is called"


# ===========================================================================
# cancel_reopen()
# ===========================================================================

class TestCancelReopen:
    """cancel_reopen() reverses a reopen — back to completed."""

    async def test_cancel_reopen_succeeds_on_reopened_task(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/cancel-reopen-ok",
            project_id="test-project",
            goal="Cancel reopen me",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await tasks.reopen_task(task["id"])

        result = await tasks.cancel_reopen(task["id"])
        assert result["status"] == "completed"

    async def test_cancel_reopen_fails_on_non_reopened_task(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/cancel-reopen-fail",
            project_id="test-project",
            goal="Not reopened",
        )
        await db.update_task(task["id"], status="completed")

        with pytest.raises(ValueError, match="must be 'reopened'"):
            await tasks.cancel_reopen(task["id"])

    async def test_cancel_reopen_fails_on_missing_task(self, db, sample_project):
        import tasks

        with pytest.raises(ValueError, match="not found"):
            await tasks.cancel_reopen("test-project/no-such-task")

    async def test_cancel_reopen_decrements_attempt(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/cancel-reopen-attempt",
            project_id="test-project",
            goal="Decrement attempt",
        )
        await db.update_task(task["id"], status="completed", current_attempt=2)
        await tasks.reopen_task(task["id"])

        # After reopen, current_attempt=3
        reopened = await db.get_task(task["id"])
        assert reopened["current_attempt"] == 3

        await tasks.cancel_reopen(task["id"])

        reverted = await db.get_task(task["id"])
        assert reverted["current_attempt"] == 2

    async def test_cancel_reopen_deletes_reopened_messages(self, db, sample_project):
        """Messages stamped to the reopened attempt should be deleted."""
        import tasks

        task = await db.create_task(
            id="test-project/cancel-reopen-msgs",
            project_id="test-project",
            goal="Message cleanup check",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await tasks.reopen_task(task["id"])

        # Post some feedback during the reopened state
        await db.post_task_message(
            task_id=task["id"], author="stephen", content="Some feedback."
        )

        thread_before = await db.read_task_messages(task["id"])
        msgs_before = thread_before["messages"]
        attempt2_msgs = [m for m in msgs_before if m.get("attempt_number") == 2]
        assert len(attempt2_msgs) > 0

        await tasks.cancel_reopen(task["id"])

        thread_after = await db.read_task_messages(task["id"])
        msgs_after = thread_after["messages"]
        attempt2_msgs_after = [m for m in msgs_after if m.get("attempt_number") == 2]
        assert len(attempt2_msgs_after) == 0

    async def test_cancel_reopen_preserves_earlier_attempt_messages(self, db, sample_project):
        """Messages from attempt 1 should NOT be deleted."""
        import tasks

        task = await db.create_task(
            id="test-project/cancel-reopen-preserve",
            project_id="test-project",
            goal="Preserve old messages",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)

        # Post a message before reopen (attempt 1)
        await db.post_task_message(
            task_id=task["id"], author="cc-worker", type="result", content="Done!"
        )

        await tasks.reopen_task(task["id"])
        await tasks.cancel_reopen(task["id"])

        thread = await db.read_task_messages(task["id"])
        msgs = thread["messages"]
        attempt1_msgs = [m for m in msgs if (m.get("attempt_number") or 1) == 1]
        assert len(attempt1_msgs) > 0


# ===========================================================================
# _sync_branch_with_base()
# ===========================================================================

class TestSyncBranchWithBase:
    """_sync_branch_with_base() — rebase helper for start_reopened_task."""

    async def test_returns_true_when_no_worktree(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/sync-no-worktree",
            project_id="test-project",
            goal="No worktree",
        )
        # No worktree_path set — should succeed immediately
        result = await tasks._sync_branch_with_base(task)
        assert result is True

    async def test_returns_true_when_worktree_missing_from_disk(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/sync-missing-dir",
            project_id="test-project",
            goal="Missing dir",
        )
        await db.update_task(task["id"], worktree_path="/nonexistent/path/that/does/not/exist")
        task = await db.get_task(task["id"])

        result = await tasks._sync_branch_with_base(task)
        assert result is True

    async def test_rebase_conflict_sets_gate_status_needs_review(self, db, sample_project):
        """When rebase fails, gate_status is set to needs-review."""
        import os
        import tasks

        task = await db.create_task(
            id="test-project/sync-conflict",
            project_id="test-project",
            goal="Rebase conflict",
        )
        # Fake a real worktree path using /tmp (exists on disk)
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            await db.update_task(task["id"], worktree_path=tmpdir)
            task = await db.get_task(task["id"])

            # Mock _run_as_worker to fail rebase
            async def mock_run(*cmd, **kwargs):
                # fetch succeeds, rebase fails, abort succeeds
                if "rebase" in cmd and "--abort" not in cmd:
                    return b"", b"CONFLICT", 1
                return b"", b"", 0

            with patch("tasks._run_as_worker", mock_run):
                with patch("tasks.resolve_branch_target", return_value="main"):
                    result = await tasks._sync_branch_with_base(task)

        assert result is False
        updated = await db.get_task(task["id"])
        assert updated["gate_status"] == "needs-review"

    async def test_rebase_conflict_posts_error_message(self, db, sample_project):
        """When rebase fails, an error message is posted to the thread."""
        import tasks
        import tempfile
        from unittest.mock import patch

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

            with patch("tasks._run_as_worker", mock_run):
                with patch("tasks.resolve_branch_target", return_value="main"):
                    await tasks._sync_branch_with_base(task)

        thread = await db.read_task_messages(task["id"])
        msgs = thread["messages"]
        conflict_msgs = [m for m in msgs if "conflict" in (m.get("title") or "").lower()]
        assert len(conflict_msgs) == 1


# ===========================================================================
# start_reopened_task() — auto_test/auto_review overrides
# ===========================================================================

class TestStartReopenedTaskOverrides:
    """start_reopened_task passes per-dispatch overrides to dispatch_task."""

    async def test_start_passes_auto_test_override(self, db, sample_project):
        import tasks

        task = await db.create_task(
            id="test-project/start-override-test",
            project_id="test-project",
            goal="Override auto_test",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await tasks.reopen_task(task["id"])

        mock_dispatch = AsyncMock(return_value={"status": "working"})
        with patch("tasks.dispatch_task", mock_dispatch):
            with patch("tasks._invalidate_chain", AsyncMock()):
                with patch("tasks._sync_branch_with_base", AsyncMock(return_value=True)):
                    with patch("tasks.notify.task_attempt_starting", AsyncMock()):
                        await tasks.start_reopened_task(task["id"], auto_test=False)

        call_kwargs = mock_dispatch.await_args.kwargs
        assert call_kwargs["auto_test"] is False

    async def test_start_without_overrides_omits_auto_test_key(self, db, sample_project):
        """When no overrides given, dispatch_task is called without auto_test."""
        import tasks

        task = await db.create_task(
            id="test-project/start-no-override",
            project_id="test-project",
            goal="No override",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await tasks.reopen_task(task["id"])

        mock_dispatch = AsyncMock(return_value={"status": "working"})
        with patch("tasks.dispatch_task", mock_dispatch):
            with patch("tasks._invalidate_chain", AsyncMock()):
                with patch("tasks._sync_branch_with_base", AsyncMock(return_value=True)):
                    with patch("tasks.notify.task_attempt_starting", AsyncMock()):
                        await tasks.start_reopened_task(task["id"])

        call_kwargs = mock_dispatch.await_args.kwargs
        assert "auto_test" not in call_kwargs
        assert "auto_review" not in call_kwargs
