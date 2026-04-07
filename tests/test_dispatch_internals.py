"""Tests for switchboard.dispatch.internals — status-agnostic dispatch building blocks.

Each extracted function is tested in isolation with mocked DB/git operations.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import switchboard.db as db


# ---------------------------------------------------------------------------
# setup_task_worktree
# ---------------------------------------------------------------------------

class TestSetupTaskWorktree:
    @pytest.fixture(autouse=True)
    def _patches(self):
        self.setup_worktree_mock = AsyncMock(return_value="/tmp/fake-worktree")
        self.setup_hook_mock = AsyncMock()
        self.run_setup_mock = AsyncMock()
        # Patch on engine's namespace since internals reads through engine.*
        patches = [
            patch("switchboard.dispatch.engine.setup_worktree", self.setup_worktree_mock),
            patch("switchboard.dispatch.internals.setup_hook_config", self.setup_hook_mock),
            patch("switchboard.dispatch.engine.run_setup_command", self.run_setup_mock),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_creates_worktree(self, db, sample_project):
        from switchboard.dispatch.internals import setup_task_worktree

        task = await db.create_task(
            id="test-project/my-task", project_id="test-project",
            goal="Test", branch="my-task",
        )
        result = await setup_task_worktree(sample_project, task)

        assert result == "/tmp/fake-worktree"
        self.setup_worktree_mock.assert_awaited_once()
        self.setup_hook_mock.assert_awaited_once_with("/tmp/fake-worktree")
        self.run_setup_mock.assert_awaited_once_with(sample_project, "/tmp/fake-worktree")

    async def test_idempotent_reuses_worktree(self, db, sample_project):
        """If worktree already exists, setup_worktree handles it — we just call through."""
        from switchboard.dispatch.internals import setup_task_worktree

        task = await db.create_task(
            id="test-project/reuse-task", project_id="test-project",
            goal="Test", branch="reuse-task",
        )
        # Call twice — both should succeed
        path1 = await setup_task_worktree(sample_project, task)
        path2 = await setup_task_worktree(sample_project, task)
        assert path1 == path2
        assert self.setup_worktree_mock.await_count == 2

    async def test_updates_branch_if_none(self, db, sample_project):
        """If task.branch is None, effective_branch is computed from task_id."""
        from switchboard.dispatch.internals import setup_task_worktree

        task = await db.create_task(
            id="test-project/auto-branch", project_id="test-project",
            goal="Test", branch=None,
        )
        await setup_task_worktree(sample_project, task)

        # Should have updated branch in DB
        updated = await db.get_task("test-project/auto-branch")
        assert updated["branch"] == "auto-branch"


# ---------------------------------------------------------------------------
# resolve_session_config
# ---------------------------------------------------------------------------

class TestResolveSessionConfig:
    def test_task_overrides_project(self):
        from switchboard.dispatch.internals import resolve_session_config

        task = {"max_turns": 50, "max_wall_clock": 30, "model": "haiku"}
        project = {"max_turns": 150, "max_wall_clock": 45, "model": "opus"}
        config = resolve_session_config(task, project)

        assert config["max_turns"] == 50
        assert config["max_wall_clock"] == 30
        assert config["model"] == "haiku"

    def test_project_defaults(self):
        from switchboard.dispatch.internals import resolve_session_config

        task = {"max_turns": None, "max_wall_clock": None, "model": None}
        project = {"max_turns": 150, "max_wall_clock": 45, "model": "opus"}
        config = resolve_session_config(task, project)

        assert config["max_turns"] == 150
        assert config["max_wall_clock"] == 45
        assert config["model"] == "opus"

    def test_global_defaults(self):
        from switchboard.dispatch.internals import resolve_session_config

        task = {"max_turns": None, "max_wall_clock": None, "model": None}
        project = {"max_turns": None, "max_wall_clock": None, "model": None}
        config = resolve_session_config(task, project)

        assert config["max_turns"] == db.DEFAULT_MAX_TURNS
        assert config["max_wall_clock"] == db.DEFAULT_MAX_WALL_CLOCK
        assert config["model"] == "sonnet"  # DEFAULT_MODEL


# ---------------------------------------------------------------------------
# build_dispatch_prompt
# ---------------------------------------------------------------------------

class TestBuildDispatchPrompt:
    @pytest.fixture(autouse=True)
    def _patches(self):
        self.build_prompt_mock = AsyncMock(return_value="mock prompt")
        patches = [
            patch("switchboard.dispatch.engine._build_task_prompt", self.build_prompt_mock),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_with_spec(self, db, sample_project):
        from switchboard.dispatch.internals import build_dispatch_prompt

        task = await db.create_task(
            id="test-project/prompt-task", project_id="test-project",
            goal="Test",
        )
        # Pin a spec message
        await db.post_task_message(
            task_id="test-project/prompt-task", author="dispatcher",
            content="Build the feature", type="spec", pinned=True,
        )

        result = await build_dispatch_prompt(sample_project, task)
        assert result == "mock prompt"

        # Verify _build_task_prompt was called with spec_content
        call_args = self.build_prompt_mock.call_args
        assert call_args[0][2] == "Build the feature"  # spec_content

    async def test_with_feedback(self, db, sample_project):
        from switchboard.dispatch.internals import build_dispatch_prompt

        task = await db.create_task(
            id="test-project/feedback-task", project_id="test-project",
            goal="Test",
        )
        feedback = [{"author": "reviewer", "content": "Fix the bug"}]
        await build_dispatch_prompt(sample_project, task, review_feedback=feedback)

        call_args = self.build_prompt_mock.call_args
        assert call_args[0][5] == feedback  # review_feedback param


# ---------------------------------------------------------------------------
# launch_sdk_session
# ---------------------------------------------------------------------------

class TestLaunchSdkSession:
    @pytest.fixture(autouse=True)
    def _patches(self):
        self.setup_log_mock = AsyncMock(return_value="/tmp/log-dir")
        self.write_log_mock = MagicMock()
        self.run_session_mock = AsyncMock()
        self.handle_exc_mock = MagicMock()
        patches = [
            patch("switchboard.dispatch.engine._setup_log_dir", self.setup_log_mock),
            patch("switchboard.dispatch.engine._write_dispatch_log", self.write_log_mock),
            patch("switchboard.dispatch.engine._run_sdk_session", self.run_session_mock),
            patch("switchboard.dispatch.engine._handle_task_exception", self.handle_exc_mock),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_adds_to_running_tasks(self):
        from switchboard.dispatch.internals import launch_sdk_session
        from switchboard.dispatch._state import _running_tasks

        initial_count = len(_running_tasks)
        task_handle = await launch_sdk_session(
            task_id="test/task", prompt="do stuff",
            worktree_path="/tmp/wt",
        )

        assert isinstance(task_handle, asyncio.Task)
        assert task_handle in _running_tasks

        # Cleanup
        task_handle.cancel()
        try:
            await task_handle
        except (asyncio.CancelledError, Exception):
            pass
        _running_tasks.discard(task_handle)

    async def test_with_session_id(self):
        from switchboard.dispatch.internals import launch_sdk_session

        task_handle = await launch_sdk_session(
            task_id="test/resume", prompt="continue",
            worktree_path="/tmp/wt",
            session_id="sess-123", is_resume=True,
        )

        # Verify _run_sdk_session was called with session_id and is_resume
        call_kwargs = self.run_session_mock.call_args[1]
        assert call_kwargs["session_id"] == "sess-123"
        assert call_kwargs["is_resume"] is True

        # Cleanup
        task_handle.cancel()
        try:
            await task_handle
        except (asyncio.CancelledError, Exception):
            pass
        from switchboard.dispatch._state import _running_tasks
        _running_tasks.discard(task_handle)


# ---------------------------------------------------------------------------
# check_and_queue_if_full
# ---------------------------------------------------------------------------

class TestCheckAndQueueIfFull:
    async def test_queues_when_full(self, db, sample_project):
        from switchboard.dispatch.internals import check_and_queue_if_full

        task = await db.create_task(
            id="test-project/queue-task", project_id="test-project",
            goal="Test",
        )

        with patch.object(db, "count_active_tasks", AsyncMock(return_value=5)), \
             patch.object(db, "get_concurrency_limit", AsyncMock(return_value=5)):
            result = await check_and_queue_if_full("test-project/queue-task")

        assert result is True
        updated = await db.get_task("test-project/queue-task")
        assert updated["queued_at"] is not None

    async def test_available_when_not_full(self, db, sample_project):
        from switchboard.dispatch.internals import check_and_queue_if_full

        task = await db.create_task(
            id="test-project/open-task", project_id="test-project",
            goal="Test",
        )

        with patch.object(db, "count_active_tasks", AsyncMock(return_value=2)), \
             patch.object(db, "get_concurrency_limit", AsyncMock(return_value=5)):
            result = await check_and_queue_if_full("test-project/open-task")

        assert result is False


# ---------------------------------------------------------------------------
# collect_review_feedback
# ---------------------------------------------------------------------------

class TestCollectReviewFeedback:
    async def test_found(self, db, sample_project):
        from switchboard.dispatch.internals import collect_review_feedback

        task = await db.create_task(
            id="test-project/review-fb", project_id="test-project",
            goal="Test",
        )
        # Post a result message then feedback
        await db.post_task_message(
            task_id="test-project/review-fb", author="cc-worker",
            content="Done", type="result",
        )
        await db.post_task_message(
            task_id="test-project/review-fb", author="user",
            content="Please fix the typo", type="note",
        )

        result = await collect_review_feedback("test-project/review-fb")
        assert result is not None
        assert len(result) == 1
        assert result[0]["content"] == "Please fix the typo"

    async def test_none(self, db, sample_project):
        from switchboard.dispatch.internals import collect_review_feedback

        task = await db.create_task(
            id="test-project/no-fb", project_id="test-project",
            goal="Test",
        )
        # Post a result but no feedback after
        await db.post_task_message(
            task_id="test-project/no-fb", author="cc-worker",
            content="Done", type="result",
        )

        result = await collect_review_feedback("test-project/no-fb")
        assert result is None

    async def test_ignores_dispatcher_messages(self, db, sample_project):
        from switchboard.dispatch.internals import collect_review_feedback

        task = await db.create_task(
            id="test-project/dispatcher-fb", project_id="test-project",
            goal="Test",
        )
        await db.post_task_message(
            task_id="test-project/dispatcher-fb", author="cc-worker",
            content="Done", type="result",
        )
        # Dispatcher status message should be ignored
        await db.post_task_message(
            task_id="test-project/dispatcher-fb", author="dispatcher",
            content="Status update", type="status",
        )

        result = await collect_review_feedback("test-project/dispatcher-fb")
        assert result is None


# ---------------------------------------------------------------------------
# collect_reopen_feedback
# ---------------------------------------------------------------------------

class TestCollectReopenFeedback:
    async def test_found(self, db, sample_project):
        from switchboard.dispatch.internals import collect_reopen_feedback

        task = await db.create_task(
            id="test-project/reopen-fb", project_id="test-project",
            goal="Test",
        )
        # Simulate attempt 1 message
        await db.post_task_message(
            task_id="test-project/reopen-fb", author="cc-worker",
            content="Done", type="result",
        )
        # Update to attempt 2 (simulating reopen)
        await db.update_task("test-project/reopen-fb", current_attempt=2)
        # Reopen status message stamped to attempt 2
        await db.post_task_message(
            task_id="test-project/reopen-fb", author="switchboard",
            content="Reopened", type="status",
        )
        # User feedback
        await db.post_task_message(
            task_id="test-project/reopen-fb", author="user",
            content="Fix the layout", type="note",
        )

        result = await collect_reopen_feedback("test-project/reopen-fb", current_attempt=2)
        assert result is not None
        assert len(result) == 1
        assert result[0]["content"] == "Fix the layout"

    async def test_none(self, db, sample_project):
        from switchboard.dispatch.internals import collect_reopen_feedback

        task = await db.create_task(
            id="test-project/reopen-nofb", project_id="test-project",
            goal="Test",
        )
        await db.update_task("test-project/reopen-nofb", current_attempt=2)
        await db.post_task_message(
            task_id="test-project/reopen-nofb", author="switchboard",
            content="Reopened", type="status",
        )
        # No user feedback — only system messages

        result = await collect_reopen_feedback("test-project/reopen-nofb", current_attempt=2)
        assert result is None
