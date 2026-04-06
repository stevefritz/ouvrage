"""Tests for session forking on retry.

Verifies that:
1. Retry forks from the previous attempt's session_id
2. Start after reopen forks from the previous attempt's session_id
3. Resume continues the current session linearly (unchanged)
4. First dispatch creates attempt record and launches fresh
5. Fallback to fresh when no previous session_id exists
6. task_attempts CRUD works correctly
"""

from unittest.mock import AsyncMock, patch

import pytest

from switchboard.dispatch.engine import retry_task


_INTERNALS = "switchboard.dispatch.internals"
_LIFECYCLE = "switchboard.dispatch.lifecycle"


class TestAttemptCRUD:
    """Test the task_attempts table CRUD operations."""

    async def test_create_attempt(self, db, sample_project):
        task = await db.create_task(
            id="test-project/attempt-crud",
            project_id="test-project",
            goal="Test attempts",
        )
        attempt = await db.create_attempt(task["id"], 1)
        assert attempt["task_id"] == task["id"]
        assert attempt["attempt_number"] == 1
        assert attempt["session_id"] is None

    async def test_update_attempt_session_id(self, db, sample_project):
        task = await db.create_task(
            id="test-project/attempt-update",
            project_id="test-project",
            goal="Test attempts",
        )
        await db.create_attempt(task["id"], 1)
        await db.update_attempt(task["id"], 1, session_id="sess-abc")

        attempt = await db.get_attempt(task["id"], 1)
        assert attempt["session_id"] == "sess-abc"

    async def test_get_previous_attempt_session_id(self, db, sample_project):
        task = await db.create_task(
            id="test-project/attempt-prev",
            project_id="test-project",
            goal="Test attempts",
        )
        await db.create_attempt(task["id"], 1)
        await db.update_attempt(task["id"], 1, session_id="sess-111")
        await db.create_attempt(task["id"], 2)

        # Attempt 2 should see attempt 1's session_id
        prev = await db.get_previous_attempt_session_id(task["id"], 2)
        assert prev == "sess-111"

    async def test_get_previous_attempt_session_id_none(self, db, sample_project):
        task = await db.create_task(
            id="test-project/attempt-prev-none",
            project_id="test-project",
            goal="Test attempts",
        )
        # No attempt records at all
        prev = await db.get_previous_attempt_session_id(task["id"], 1)
        assert prev is None

    async def test_get_previous_attempt_no_session(self, db, sample_project):
        """Previous attempt exists but has no session_id (crashed before init)."""
        task = await db.create_task(
            id="test-project/attempt-no-sess",
            project_id="test-project",
            goal="Test attempts",
        )
        await db.create_attempt(task["id"], 1)
        # session_id remains None
        await db.create_attempt(task["id"], 2)

        prev = await db.get_previous_attempt_session_id(task["id"], 2)
        assert prev is None

    async def test_unique_constraint(self, db, sample_project):
        task = await db.create_task(
            id="test-project/attempt-unique",
            project_id="test-project",
            goal="Test unique constraint",
        )
        await db.create_attempt(task["id"], 1)
        with pytest.raises(Exception):  # IntegrityError
            await db.create_attempt(task["id"], 1)


class TestRetryFork:
    """Retry should fork from the previous attempt's session."""

    @pytest.fixture(autouse=True)
    def _base_patches(self):
        patches = [
            patch("switchboard.dispatch.engine.archive_task_logs", AsyncMock()),
            patch("switchboard.dispatch.engine.db.revert_punchlist_items_for_task", AsyncMock(return_value=0)),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
            patch(f"{_INTERNALS}.collect_review_feedback", AsyncMock(return_value=None)),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_retry_forks_from_previous_attempt(self, db, sample_project):
        """Retry passes fork_session_id from previous attempt to launch_sdk_session."""
        task = await db.create_task(
            id="test-project/fork-retry",
            project_id="test-project",
            goal="Test fork on retry",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1,
                             session_id="sess-original")
        # Create attempt 1 with a session_id
        await db.create_attempt(task["id"], 1)
        await db.update_attempt(task["id"], 1, session_id="sess-original")

        mock_launch = AsyncMock()
        with (
            patch(f"{_INTERNALS}.setup_task_worktree", AsyncMock(return_value="/tmp/wt")),
            patch(f"{_INTERNALS}.build_dispatch_prompt", AsyncMock(return_value="prompt")),
            patch(f"{_INTERNALS}.launch_sdk_session", mock_launch),
        ):
            await retry_task("test-project/fork-retry")

        # launch_sdk_session should have been called with fork_session_id
        mock_launch.assert_called_once()
        call_kwargs = mock_launch.call_args
        assert call_kwargs.kwargs.get("fork_session_id") == "sess-original"
        # Should NOT be a resume
        assert call_kwargs.kwargs.get("session_id") is None or "session_id" not in call_kwargs.kwargs

    async def test_retry_fallback_to_task_session_id(self, db, sample_project):
        """When no attempt record exists, retry falls back to task-level session_id."""
        task = await db.create_task(
            id="test-project/fork-fallback",
            project_id="test-project",
            goal="Test fork fallback",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1,
                             session_id="sess-task-level")
        # No attempt record created — simulates pre-migration task

        mock_launch = AsyncMock()
        with (
            patch(f"{_INTERNALS}.setup_task_worktree", AsyncMock(return_value="/tmp/wt")),
            patch(f"{_INTERNALS}.build_dispatch_prompt", AsyncMock(return_value="prompt")),
            patch(f"{_INTERNALS}.launch_sdk_session", mock_launch),
        ):
            await retry_task("test-project/fork-fallback")

        mock_launch.assert_called_once()
        call_kwargs = mock_launch.call_args
        assert call_kwargs.kwargs.get("fork_session_id") == "sess-task-level"

    async def test_retry_fresh_when_no_session(self, db, sample_project):
        """When previous attempt has no session_id, retry launches fresh."""
        task = await db.create_task(
            id="test-project/fork-fresh",
            project_id="test-project",
            goal="Test fresh fallback",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)
        # No session_id anywhere

        mock_launch = AsyncMock()
        with (
            patch(f"{_INTERNALS}.setup_task_worktree", AsyncMock(return_value="/tmp/wt")),
            patch(f"{_INTERNALS}.build_dispatch_prompt", AsyncMock(return_value="prompt")),
            patch(f"{_INTERNALS}.launch_sdk_session", mock_launch),
        ):
            await retry_task("test-project/fork-fresh")

        mock_launch.assert_called_once()
        call_kwargs = mock_launch.call_args
        assert call_kwargs.kwargs.get("fork_session_id") is None

    async def test_retry_creates_attempt_record(self, db, sample_project):
        """Retry creates a new attempt record for the new attempt."""
        task = await db.create_task(
            id="test-project/fork-attempt-record",
            project_id="test-project",
            goal="Test attempt record creation",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1)

        with (
            patch(f"{_INTERNALS}.setup_task_worktree", AsyncMock(return_value="/tmp/wt")),
            patch(f"{_INTERNALS}.build_dispatch_prompt", AsyncMock(return_value="prompt")),
            patch(f"{_INTERNALS}.launch_sdk_session", AsyncMock()),
        ):
            await retry_task("test-project/fork-attempt-record")

        # Attempt 2 record should exist
        attempt = await db.get_attempt("test-project/fork-attempt-record", 2)
        assert attempt is not None
        assert attempt["attempt_number"] == 2

    async def test_retry_does_not_clear_session_id(self, db, sample_project):
        """Retry no longer clears session_id on the task record."""
        task = await db.create_task(
            id="test-project/fork-no-clear",
            project_id="test-project",
            goal="Test session_id preservation",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1,
                             session_id="sess-preserved")
        await db.create_attempt(task["id"], 1)
        await db.update_attempt(task["id"], 1, session_id="sess-preserved")

        with (
            patch(f"{_INTERNALS}.setup_task_worktree", AsyncMock(return_value="/tmp/wt")),
            patch(f"{_INTERNALS}.build_dispatch_prompt", AsyncMock(return_value="prompt")),
            patch(f"{_INTERNALS}.launch_sdk_session", AsyncMock()),
        ):
            await retry_task("test-project/fork-no-clear")

        stored = await db.get_task("test-project/fork-no-clear")
        # session_id should NOT have been cleared to None
        assert stored["session_id"] == "sess-preserved"

    async def test_retry_with_fresh_option_skips_fork(self, db, sample_project):
        """When fresh=True, retry launches without fork_session_id even if previous session exists."""
        task = await db.create_task(
            id="test-project/fork-fresh-option",
            project_id="test-project",
            goal="Test fresh=True skips fork",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1,
                             session_id="sess-previous")
        await db.create_attempt(task["id"], 1)
        await db.update_attempt(task["id"], 1, session_id="sess-previous")

        mock_launch = AsyncMock()
        with (
            patch(f"{_INTERNALS}.setup_task_worktree", AsyncMock(return_value="/tmp/wt")),
            patch(f"{_INTERNALS}.build_dispatch_prompt", AsyncMock(return_value="prompt")),
            patch(f"{_INTERNALS}.launch_sdk_session", mock_launch),
        ):
            await retry_task("test-project/fork-fresh-option", fresh=True)

        mock_launch.assert_called_once()
        call_kwargs = mock_launch.call_args
        assert call_kwargs.kwargs.get("fork_session_id") is None

    async def test_retry_with_fresh_false_still_forks(self, db, sample_project):
        """When fresh=False (default), retry still forks from the previous session."""
        task = await db.create_task(
            id="test-project/fork-fresh-false",
            project_id="test-project",
            goal="Test fresh=False keeps fork",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1,
                             session_id="sess-to-fork")
        await db.create_attempt(task["id"], 1)
        await db.update_attempt(task["id"], 1, session_id="sess-to-fork")

        mock_launch = AsyncMock()
        with (
            patch(f"{_INTERNALS}.setup_task_worktree", AsyncMock(return_value="/tmp/wt")),
            patch(f"{_INTERNALS}.build_dispatch_prompt", AsyncMock(return_value="prompt")),
            patch(f"{_INTERNALS}.launch_sdk_session", mock_launch),
        ):
            await retry_task("test-project/fork-fresh-false", fresh=False)

        mock_launch.assert_called_once()
        call_kwargs = mock_launch.call_args
        assert call_kwargs.kwargs.get("fork_session_id") == "sess-to-fork"


class TestStartFork:
    """Start after reopen should fork from the previous attempt's session."""

    @pytest.fixture(autouse=True)
    def _base_patches(self):
        patches = [
            patch(f"{_INTERNALS}.collect_reopen_feedback", AsyncMock(return_value=None)),
            patch("switchboard.git.operations._sync_branch_with_base", AsyncMock()),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_start_forks_from_previous_attempt(self, db, sample_project):
        """Start after reopen passes fork_session_id from previous attempt."""
        task = await db.create_task(
            id="test-project/start-fork",
            project_id="test-project",
            goal="Test fork on start",
        )
        # Simulate: completed with session_id, then reopened (awaiting feedback)
        await db.update_task(task["id"], status="reopened", current_attempt=2,
                             session_id="sess-completed",
                             reason="awaiting_feedback")
        # Attempt 1 had a session_id
        await db.create_attempt(task["id"], 1)
        await db.update_attempt(task["id"], 1, session_id="sess-completed")
        # Attempt 2 created by reopen
        await db.create_attempt(task["id"], 2)

        mock_launch = AsyncMock()
        from switchboard.dispatch.lifecycle import lifecycle

        with (
            patch(f"{_INTERNALS}.setup_task_worktree", AsyncMock(return_value="/tmp/wt")),
            patch(f"{_INTERNALS}.build_dispatch_prompt", AsyncMock(return_value="prompt")),
            patch(f"{_INTERNALS}.launch_sdk_session", mock_launch),
        ):
            await lifecycle.execute(task["id"], "start")

        mock_launch.assert_called_once()
        call_kwargs = mock_launch.call_args
        assert call_kwargs.kwargs.get("fork_session_id") == "sess-completed"


class TestReopenPreservesSession:
    """Reopen should NOT clear session_id so start can fork from it."""

    async def test_reopen_preserves_session_id(self, db, sample_project):
        """Reopen no longer clears session_id on the task record."""
        task = await db.create_task(
            id="test-project/reopen-preserve",
            project_id="test-project",
            goal="Test reopen session preservation",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1,
                             session_id="sess-to-keep", gate_status="passed",
                             gate_passed_at="2026-01-01T00:00:00Z")

        from switchboard.dispatch.lifecycle import lifecycle
        await lifecycle.execute(task["id"], "reopen")

        stored = await db.get_task("test-project/reopen-preserve")
        # session_id should be preserved
        assert stored["session_id"] == "sess-to-keep"
        # But gate state should be cleared
        assert stored["gate_status"] is None

    async def test_reopen_creates_attempt_record(self, db, sample_project):
        """Reopen creates a new attempt record."""
        task = await db.create_task(
            id="test-project/reopen-attempt",
            project_id="test-project",
            goal="Test reopen attempt creation",
        )
        await db.update_task(task["id"], status="completed", current_attempt=1,
                             gate_status="passed",
                             gate_passed_at="2026-01-01T00:00:00Z")

        from switchboard.dispatch.lifecycle import lifecycle
        await lifecycle.execute(task["id"], "reopen")

        attempt = await db.get_attempt("test-project/reopen-attempt", 2)
        assert attempt is not None
        assert attempt["attempt_number"] == 2


class TestDispatchFresh:
    """First dispatch should always be fresh (no fork)."""

    @pytest.fixture(autouse=True)
    def _base_patches(self):
        patches = [
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_dispatch_no_fork(self, db, sample_project):
        """First dispatch creates attempt record and launches with no fork."""
        task = await db.create_task(
            id="test-project/dispatch-fresh",
            project_id="test-project",
            goal="Test fresh dispatch",
        )

        mock_launch = AsyncMock()
        from switchboard.dispatch.lifecycle import lifecycle

        with (
            patch(f"{_INTERNALS}.check_and_queue_if_full", AsyncMock(return_value=False)),
            patch(f"{_INTERNALS}.setup_task_worktree", AsyncMock(return_value="/tmp/wt")),
            patch(f"{_INTERNALS}.build_dispatch_prompt", AsyncMock(return_value="prompt")),
            patch(f"{_INTERNALS}.launch_sdk_session", mock_launch),
        ):
            await lifecycle.execute(task["id"], "dispatch")

        mock_launch.assert_called_once()
        call_kwargs = mock_launch.call_args
        # No fork_session_id on first dispatch
        assert call_kwargs.kwargs.get("fork_session_id") is None or "fork_session_id" not in call_kwargs.kwargs

    async def test_dispatch_creates_attempt_record(self, db, sample_project):
        """First dispatch creates an attempt record for attempt 1."""
        task = await db.create_task(
            id="test-project/dispatch-attempt",
            project_id="test-project",
            goal="Test dispatch attempt record",
        )

        from switchboard.dispatch.lifecycle import lifecycle

        with (
            patch(f"{_INTERNALS}.check_and_queue_if_full", AsyncMock(return_value=False)),
            patch(f"{_INTERNALS}.setup_task_worktree", AsyncMock(return_value="/tmp/wt")),
            patch(f"{_INTERNALS}.build_dispatch_prompt", AsyncMock(return_value="prompt")),
            patch(f"{_INTERNALS}.launch_sdk_session", AsyncMock()),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
        ):
            await lifecycle.execute(task["id"], "dispatch")

        attempt = await db.get_attempt("test-project/dispatch-attempt", 1)
        assert attempt is not None
        assert attempt["attempt_number"] == 1


class TestResume:
    """Resume should continue current session linearly (no fork)."""

    @pytest.fixture(autouse=True)
    def _base_patches(self):
        patches = [
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_resume_uses_current_session(self, db, sample_project):
        """Resume passes session_id for linear continuation, not fork."""
        task = await db.create_task(
            id="test-project/resume-linear",
            project_id="test-project",
            goal="Test resume",
        )
        await db.update_task(task["id"], status="stopped",
                             current_attempt=1, session_id="sess-current",
                             worktree_path="/tmp/fake-wt")
        # Create attempt record with session_id
        await db.create_attempt(task["id"], 1)
        await db.update_attempt(task["id"], 1, session_id="sess-current")

        mock_launch = AsyncMock()
        from switchboard.dispatch.lifecycle import lifecycle

        with (
            patch(f"{_INTERNALS}.setup_task_worktree", AsyncMock(return_value="/tmp/wt")),
            patch(f"{_INTERNALS}.launch_sdk_session", mock_launch),
            patch("os.path.exists", return_value=True),
        ):
            await lifecycle.execute(task["id"], "resume")

        mock_launch.assert_called_once()
        call_kwargs = mock_launch.call_args
        # Should be a resume with session_id, not a fork
        assert call_kwargs.kwargs.get("session_id") == "sess-current"
        assert call_kwargs.kwargs.get("is_resume") is True
        assert call_kwargs.kwargs.get("fork_session_id") is None or "fork_session_id" not in call_kwargs.kwargs

    async def test_resume_reads_from_attempt_record(self, db, sample_project):
        """Resume reads session_id from attempt record, not just task-level."""
        task = await db.create_task(
            id="test-project/resume-attempt",
            project_id="test-project",
            goal="Test resume from attempt",
        )
        # Task-level session_id is stale; attempt record has the real one
        await db.update_task(task["id"], status="stopped",
                             current_attempt=2, session_id="sess-stale",
                             worktree_path="/tmp/fake-wt")
        await db.create_attempt(task["id"], 2)
        await db.update_attempt(task["id"], 2, session_id="sess-from-attempt")

        mock_launch = AsyncMock()
        from switchboard.dispatch.lifecycle import lifecycle

        with (
            patch(f"{_INTERNALS}.setup_task_worktree", AsyncMock(return_value="/tmp/wt")),
            patch(f"{_INTERNALS}.launch_sdk_session", mock_launch),
            patch("os.path.exists", return_value=True),
        ):
            await lifecycle.execute(task["id"], "resume")

        mock_launch.assert_called_once()
        call_kwargs = mock_launch.call_args
        assert call_kwargs.kwargs.get("session_id") == "sess-from-attempt"


class TestLaunchSdkSessionFork:
    """Test that launch_sdk_session passes fork_session_id through to _run_sdk_session."""

    async def test_fork_param_passed_through(self):
        """fork_session_id is forwarded to _run_sdk_session."""
        import switchboard.dispatch.engine as _engine

        mock_run = AsyncMock()
        mock_setup_log = AsyncMock(return_value="/tmp/log")
        mock_write_log = lambda *a, **kw: None

        with (
            patch.object(_engine, "_run_sdk_session", mock_run),
            patch.object(_engine, "_setup_log_dir", mock_setup_log),
            patch.object(_engine, "_write_dispatch_log", mock_write_log),
            patch(f"{_INTERNALS}._copy_archived_session_log", AsyncMock()),
        ):
            from switchboard.dispatch.internals import launch_sdk_session
            handle = await launch_sdk_session(
                task_id="test/fork-passthrough",
                prompt="test prompt",
                worktree_path="/tmp/wt",
                fork_session_id="sess-to-fork",
                max_turns=10,
                max_wall_clock=5,
                model="sonnet",
            )
            # Let the task run
            try:
                await handle
            except Exception:
                pass

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("fork_session_id") == "sess-to-fork"
