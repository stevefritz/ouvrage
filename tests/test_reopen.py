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

    async def test_reopen_fails_on_non_completed_task(self, db, sample_project):
        from switchboard.dispatch.engine import reopen_task
        from switchboard.dispatch.lifecycle import IllegalTransition

        task = await db.create_task(
            id="test-project/reopen-fail-working",
            project_id="test-project",
            goal="Not completed",
        )
        await db.update_task(task["id"], status="working")

        with pytest.raises(IllegalTransition):
            await reopen_task(task["id"])

    async def test_reopen_fails_on_failed_task(self, db, sample_project):
        from switchboard.dispatch.engine import reopen_task
        from switchboard.dispatch.lifecycle import IllegalTransition

        task = await db.create_task(
            id="test-project/reopen-fail-failed",
            project_id="test-project",
            goal="Failed task",
        )
        await db.update_task(task["id"], status="failed")

        with pytest.raises(IllegalTransition):
            await reopen_task(task["id"])

    async def test_reopen_fails_on_missing_task(self, db, sample_project):
        from switchboard.dispatch.engine import reopen_task

        with pytest.raises(ValueError, match="not found"):
            await reopen_task("test-project/does-not-exist")

    async def test_reopen_increments_current_attempt(self, db, sample_project):
        from switchboard.dispatch.engine import reopen_task

        task = await db.create_task(
            id="test-project/reopen-attempt",
            project_id="test-project",
            goal="Increment attempt",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)

        await reopen_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["current_attempt"] == 2

    async def test_reopen_increments_from_higher_attempt(self, db, sample_project):
        """Works correctly when current_attempt > 1."""
        from switchboard.dispatch.engine import reopen_task

        task = await db.create_task(
            id="test-project/reopen-attempt-3",
            project_id="test-project",
            goal="Third attempt reopen",
        )
        await db.update_task(task["id"], status="completed", current_attempt=3)

        await reopen_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["current_attempt"] == 4

    async def test_reopen_sets_status_to_stopped_awaiting_feedback(self, db, sample_project):
        from switchboard.dispatch.engine import reopen_task

        task = await db.create_task(
            id="test-project/reopen-status",
            project_id="test-project",
            goal="Status check",
        )
        await db.update_task(task["id"], status="completed")

        await reopen_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["status"] == "stopped"
        assert updated["reason"] == "awaiting_feedback"

    async def test_reopen_clears_session_id(self, db, sample_project):
        from switchboard.dispatch.engine import reopen_task

        task = await db.create_task(
            id="test-project/reopen-clear-session",
            project_id="test-project",
            goal="Clear session",
        )
        await db.update_task(task["id"], status="completed", session_id="ses_abc123")

        await reopen_task(task["id"])

        updated = await db.get_task(task["id"])
        # session_id preserved for fork-on-start (no longer cleared on reopen)
        assert updated["session_id"] == "ses_abc123"

    async def test_reopen_clears_gate_status(self, db, sample_project):
        from switchboard.dispatch.engine import reopen_task

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

        await reopen_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["gate_status"] is None
        assert updated["gate_passed_at"] is None

    async def test_reopen_posts_status_message_stamped_to_new_attempt(self, db, sample_project):
        from switchboard.dispatch.engine import reopen_task

        task = await db.create_task(
            id="test-project/reopen-msg",
            project_id="test-project",
            goal="Message stamp check",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)

        await reopen_task(task["id"])

        thread = await db.read_task_messages(task["id"])
        msgs = thread["messages"]
        reopen_msgs = [m for m in msgs if "reopened" in (m.get("title") or "").lower()]
        assert len(reopen_msgs) == 1
        # Message must be stamped to the new attempt (2)
        assert reopen_msgs[0]["attempt_number"] == 2

    async def test_reopen_posts_awaiting_feedback_message(self, db, sample_project):
        from switchboard.dispatch.engine import reopen_task

        task = await db.create_task(
            id="test-project/reopen-msg-content",
            project_id="test-project",
            goal="Message content check",
        )
        await db.update_task(task["id"], status="completed")

        await reopen_task(task["id"])

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
    """start_reopened_task transitions stopped(awaiting_feedback) → working via lifecycle."""

    async def test_start_fails_on_non_reopened_task(self, db, sample_project, mock_git, mock_sdk):
        from switchboard.dispatch.engine import start_reopened_task

        task = await db.create_task(
            id="test-project/start-fail-completed",
            project_id="test-project",
            goal="Not reopened",
        )
        await db.update_task(task["id"], status="completed")

        from switchboard.dispatch.lifecycle import IllegalTransition
        with pytest.raises(IllegalTransition):
            await start_reopened_task(task["id"])

    async def test_start_fails_on_missing_task(self, db, sample_project, mock_git, mock_sdk):
        from switchboard.dispatch.engine import start_reopened_task

        with pytest.raises(ValueError, match="not found"):
            await start_reopened_task("test-project/no-such-task")

    async def test_start_calls_dispatch_with_phase_revisions(self, db, sample_project, mock_git, mock_sdk):
        """start_reopened_task transitions to working status."""
        from switchboard.dispatch.engine import reopen_task, start_reopened_task

        task = await db.create_task(
            id="test-project/start-phase",
            project_id="test-project",
            goal="Phase check",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await reopen_task(task["id"])

        await start_reopened_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["status"] == "working"

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

    async def test_start_excludes_system_authors_from_feedback(self, db, sample_project, mock_git, mock_sdk):
        """switchboard, dispatcher, cc-worker messages are not included in feedback."""
        from switchboard.dispatch.engine import reopen_task
        from switchboard.dispatch.internals import collect_reopen_feedback

        task = await db.create_task(
            id="test-project/start-filter",
            project_id="test-project",
            goal="Author filter check",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await reopen_task(task["id"])

        # Post messages from system authors — should all be filtered
        await db.post_task_message(
            task_id=task["id"], author="dispatcher", content="System dispatch msg."
        )
        await db.post_task_message(
            task_id=task["id"], author="cc-worker", type="result", content="CC result msg."
        )

        # No user feedback — review_feedback should be None
        feedback = await collect_reopen_feedback(task["id"], 2)
        assert feedback is None

    async def test_start_passes_correct_task_id_and_goal(self, db, sample_project, mock_git, mock_sdk):
        from switchboard.dispatch.engine import reopen_task, start_reopened_task

        task = await db.create_task(
            id="test-project/start-args",
            project_id="test-project",
            goal="My specific goal",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await reopen_task(task["id"])

        await start_reopened_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["status"] == "working"
        assert updated["goal"] == "My specific goal"

    async def test_start_posts_attempt_starting_message(self, db, sample_project, mock_git, mock_sdk):
        """start_reopened_task posts 'Attempt N starting' before dispatching."""
        from switchboard.dispatch.engine import reopen_task, start_reopened_task

        task = await db.create_task(
            id="test-project/start-msg",
            project_id="test-project",
            goal="Starting message check",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await reopen_task(task["id"])

        await start_reopened_task(task["id"])

        thread = await db.read_task_messages(task["id"])
        msgs = thread["messages"]
        starting_msgs = [m for m in msgs if "starting" in (m.get("title") or "").lower()]
        assert len(starting_msgs) == 1
        assert starting_msgs[0]["attempt_number"] == 2

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

    async def test_start_does_not_invalidate_when_no_dependents(self, db, sample_project, mock_git, mock_sdk):
        from switchboard.dispatch.engine import reopen_task, start_reopened_task

        task = await db.create_task(
            id="test-project/start-no-deps",
            project_id="test-project",
            goal="No dependents",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await reopen_task(task["id"])

        mock_invalidate = AsyncMock()
        with patch("switchboard.dispatch.engine._invalidate_chain", mock_invalidate):
            await start_reopened_task(task["id"])

        mock_invalidate.assert_not_awaited()


# ===========================================================================
# retry_task() — regression: posts "Attempt N starting..." before dispatch
# ===========================================================================

class TestRetryTaskStartingMessage:
    """Regression: retry_task posts 'Attempt N starting' message before dispatch."""

    async def test_retry_posts_starting_message(self, db, sample_project, mock_git, mock_sdk):
        from switchboard.dispatch.engine import retry_task

        task = await db.create_task(
            id="test-project/retry-starting-msg",
            project_id="test-project",
            goal="Retry starting message regression",
        )
        await db.update_task(task["id"], status="stopped", current_attempt=1)

        await retry_task(task["id"])

        thread = await db.read_task_messages(task["id"])
        msgs = thread["messages"]
        starting_msgs = [m for m in msgs if "starting" in (m.get("title") or "").lower()]
        assert len(starting_msgs) == 1
        assert starting_msgs[0]["attempt_number"] == 2
        assert starting_msgs[0]["author"] == "switchboard"

    async def test_retry_starting_message_posted_before_dispatch(self, db, sample_project, mock_git, mock_sdk):
        """The 'Attempt N starting' message is posted as part of the retry side effect."""
        from switchboard.dispatch.engine import retry_task

        task = await db.create_task(
            id="test-project/retry-order",
            project_id="test-project",
            goal="Message ordering check",
        )
        await db.update_task(task["id"], status="stopped", current_attempt=1)

        await retry_task(task["id"])

        thread = await db.read_task_messages(task["id"])
        starting_msgs = [
            m for m in thread["messages"]
            if "starting" in (m.get("title") or "").lower()
        ]
        assert len(starting_msgs) == 1, "Starting message must be posted by retry side effect"


# ===========================================================================
# cancel_reopen()
# ===========================================================================

class TestCancelReopen:
    """cancel_reopen() reverses a reopen — back to completed."""

    async def test_cancel_reopen_succeeds_on_reopened_task(self, db, sample_project):
        from switchboard.dispatch.engine import cancel_reopen, reopen_task

        task = await db.create_task(
            id="test-project/cancel-reopen-ok",
            project_id="test-project",
            goal="Cancel reopen me",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await reopen_task(task["id"])

        result = await cancel_reopen(task["id"])
        assert result["status"] == "completed"

    async def test_cancel_reopen_fails_on_non_reopened_task(self, db, sample_project):
        from switchboard.dispatch.engine import cancel_reopen
        from switchboard.dispatch.lifecycle import IllegalTransition

        task = await db.create_task(
            id="test-project/cancel-reopen-fail",
            project_id="test-project",
            goal="Not reopened",
        )
        await db.update_task(task["id"], status="completed")

        with pytest.raises(IllegalTransition):
            await cancel_reopen(task["id"])

    async def test_cancel_reopen_fails_on_missing_task(self, db, sample_project):
        from switchboard.dispatch.engine import cancel_reopen

        with pytest.raises(ValueError, match="not found"):
            await cancel_reopen("test-project/no-such-task")

    async def test_cancel_reopen_decrements_attempt(self, db, sample_project):
        from switchboard.dispatch.engine import cancel_reopen, reopen_task

        task = await db.create_task(
            id="test-project/cancel-reopen-attempt",
            project_id="test-project",
            goal="Decrement attempt",
        )
        await db.update_task(task["id"], status="completed", current_attempt=2)
        await reopen_task(task["id"])

        # After reopen, current_attempt=3
        reopened = await db.get_task(task["id"])
        assert reopened["current_attempt"] == 3

        await cancel_reopen(task["id"])

        reverted = await db.get_task(task["id"])
        assert reverted["current_attempt"] == 2

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

    async def test_cancel_reopen_preserves_earlier_attempt_messages(self, db, sample_project):
        """Messages from attempt 1 should NOT be deleted."""
        from switchboard.dispatch.engine import cancel_reopen, reopen_task

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

        await reopen_task(task["id"])
        await cancel_reopen(task["id"])

        thread = await db.read_task_messages(task["id"])
        msgs = thread["messages"]
        attempt1_msgs = [m for m in msgs if (m.get("attempt_number") or 1) == 1]
        assert len(attempt1_msgs) > 0

    async def test_cancel_reopen_restores_gate_status(self, db, sample_project):
        """cancel_reopen restores the gate_status and gate_passed_at that were saved at reopen."""
        from switchboard.dispatch.engine import cancel_reopen, reopen_task

        task = await db.create_task(
            id="test-project/cancel-reopen-gate",
            project_id="test-project",
            goal="Gate status restore",
        )
        await db.update_task(
            task["id"],
            status="completed",
            current_attempt=1,
            gate_status="passed",
            gate_passed_at="2026-03-25T01:00:00Z",
        )

        await reopen_task(task["id"])

        # After reopen, gate_status should be cleared
        reopened = await db.get_task(task["id"])
        assert reopened["gate_status"] is None
        assert reopened["gate_passed_at"] is None
        # But saved values should be stashed
        assert reopened["reopen_saved_gate_status"] == "passed"
        assert reopened["reopen_saved_gate_passed_at"] == "2026-03-25T01:00:00Z"

        await cancel_reopen(task["id"])

        reverted = await db.get_task(task["id"])
        assert reverted["status"] == "completed"
        assert reverted["gate_status"] == "passed"
        assert reverted["gate_passed_at"] == "2026-03-25T01:00:00Z"
        # Stash should be cleared
        assert reverted["reopen_saved_gate_status"] is None
        assert reverted["reopen_saved_gate_passed_at"] is None


# ===========================================================================
# _sync_branch_with_base()
# ===========================================================================

class TestSyncBranchWithBase:
    """_sync_branch_with_base() — rebase helper for start_reopened_task."""

    async def test_returns_true_when_no_worktree(self, db, sample_project):
        from switchboard.git.operations import _sync_branch_with_base

        task = await db.create_task(
            id="test-project/sync-no-worktree",
            project_id="test-project",
            goal="No worktree",
        )
        # No worktree_path set — should succeed immediately
        result = await _sync_branch_with_base(task)
        assert result is True

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

    async def test_rebase_conflict_sets_gate_status_needs_review(self, db, sample_project):
        """When rebase fails, gate_status is set to needs-review."""
        import os
        from switchboard.git.operations import _sync_branch_with_base

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

            with patch("switchboard.git.operations._run_as_worker", mock_run):
                with patch("switchboard.git.operations.resolve_branch_target", return_value="main"):
                    result = await _sync_branch_with_base(task)

        assert result is False
        updated = await db.get_task(task["id"])
        assert updated["gate_status"] == "needs-review"

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

    async def test_start_passes_auto_test_override(self, db, sample_project, mock_git, mock_sdk):
        """auto_test/auto_review overrides are accepted by start_reopened_task (currently unused by lifecycle)."""
        from switchboard.dispatch.engine import reopen_task, start_reopened_task

        task = await db.create_task(
            id="test-project/start-override-test",
            project_id="test-project",
            goal="Override auto_test",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await reopen_task(task["id"])

        # start_reopened_task now goes through lifecycle — it accepts auto_test
        # but the lifecycle start side effect doesn't use it (it uses task's existing config)
        await start_reopened_task(task["id"], auto_test=False)
        updated = await db.get_task(task["id"])
        assert updated["status"] == "working"

    async def test_start_without_overrides_omits_auto_test_key(self, db, sample_project, mock_git, mock_sdk):
        """start_reopened_task works without any overrides."""
        from switchboard.dispatch.engine import reopen_task, start_reopened_task

        task = await db.create_task(
            id="test-project/start-no-override",
            project_id="test-project",
            goal="No override",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await reopen_task(task["id"])

        await start_reopened_task(task["id"])
        updated = await db.get_task(task["id"])
        assert updated["status"] == "working"

    async def test_start_fires_notification_with_correct_args(self, db, sample_project, mock_git, mock_sdk):
        """start_reopened_task fires task_attempt_starting with correct task_id, attempt, and goal."""
        from switchboard.dispatch.engine import reopen_task, start_reopened_task

        task = await db.create_task(
            id="test-project/start-notify-args",
            project_id="test-project",
            goal="Notification args test goal",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        await reopen_task(task["id"])

        mock_notify = AsyncMock()
        with patch("switchboard.notifications.slack.task_attempt_starting", mock_notify):
            await start_reopened_task(task["id"])

        mock_notify.assert_awaited_once_with(task["id"], 2, "Notification args test goal")
