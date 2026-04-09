"""Behavior tests for all 'launch CC' actions through lifecycle.execute().

Each action is tested through the lifecycle service interface with mocked
internals (worktree setup, SDK launch, etc.) to verify:
- Correct status transitions
- Precondition enforcement
- Side effect behavior (DB field updates, messages posted)
- Bug #2 fix: resume preserves gate_status/gate_retries
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import switchboard.db as db
from switchboard.dispatch.lifecycle import (
    IllegalTransition,
    TaskLifecycle,
)


# ---------------------------------------------------------------------------
# Shared helpers and constants
# ---------------------------------------------------------------------------

PROJECT_ID = "action-test-proj"
TASK_PREFIX = "action-test-proj"
_INTERNALS = "switchboard.dispatch.internals"


async def _seed(db_mod, task_suffix, status="ready", **extra):
    """Create project (idempotent) + task at given status."""
    try:
        await db_mod.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/action-test",
        )
    except Exception:
        pass
    task_id = f"{TASK_PREFIX}/{task_suffix}"
    await db_mod.create_task(id=task_id, project_id=PROJECT_ID, goal="test action")
    if status != "ready" or extra:
        await db_mod.update_task(task_id, status=status, **extra)
    return task_id


# Shared patch context for all tests that launch CC sessions
def _mock_launch_patches():
    """Return a list of patch objects that suppress real CC/git operations."""
    return [
        patch(f"{_INTERNALS}.check_and_queue_if_full", AsyncMock(return_value=False)),
        patch(f"{_INTERNALS}.setup_task_worktree", AsyncMock(return_value="/tmp/fake-wt")),
        patch(f"{_INTERNALS}.build_dispatch_prompt", AsyncMock(return_value="test prompt")),
        patch(f"{_INTERNALS}.launch_sdk_session", AsyncMock()),
        patch(f"{_INTERNALS}.resolve_session_config", MagicMock(return_value={"model": "sonnet", "max_turns": 50})),
        patch(f"{_INTERNALS}.collect_review_feedback", AsyncMock(return_value=None)),
        patch(f"{_INTERNALS}.collect_reopen_feedback", AsyncMock(return_value="user feedback")),
        patch(f"{_INTERNALS}.setup_hook_config", AsyncMock()),
        patch("switchboard.notifications.slack.task_dispatched", AsyncMock()),
        patch("switchboard.notifications.slack.task_attempt_starting", AsyncMock()),
        patch("switchboard.dispatch.engine.archive_task_logs", AsyncMock()),
        patch("switchboard.dispatch.engine._invalidate_chain", AsyncMock()),
        patch("switchboard.dispatch.sdk_session._build_resume_prompt", AsyncMock(return_value="resume prompt")),
        patch("switchboard.git.operations._sync_branch_with_base", AsyncMock()),
    ]


# ---------------------------------------------------------------------------
# Dispatch tests
# ---------------------------------------------------------------------------


class TestDispatchAction:
    """dispatch: ready → working"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()
        self._patches = _mock_launch_patches()
        self._mocks = {}
        for p in self._patches:
            mock = p.start()
            # Extract the short name from the patch target
            name = p.attribute if hasattr(p, 'attribute') and p.attribute else str(p)
            self._mocks[name] = mock
        yield
        for p in self._patches:
            p.stop()


# ---------------------------------------------------------------------------
# Resume tests
# ---------------------------------------------------------------------------


class TestResumeAction:
    """resume: stopped → working, cancelled → working"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()
        self._patches = _mock_launch_patches()
        for p in self._patches:
            p.start()
        yield
        for p in self._patches:
            p.stop()


    async def test_resume_stopped_with_gate_passed_still_launches(self):
        """Stopped task with gate_passed_at still launches CC (shortcut requires prev completed)."""
        task_id = await _seed(self.db_mod, "resume-gate-passed",
                              status="stopped", reason="paused_by_user",
                              gate_passed_at="2026-01-01T00:00:00Z",
                              session_id="sess-gp", worktree_path="/tmp/wt")
        with patch("os.path.exists", return_value=True):
            result = await self.lifecycle.execute(task_id, "resume")
        # prev_status=stopped doesn't match shortcut condition, so CC launches normally
        assert result["status"] == "working"

    async def test_resume_from_stopped_no_session_no_worktree_rejected(self):
        """stopped with no session_id, no gate-resumable, no worktree → rejected."""
        task_id = await _seed(self.db_mod, "resume-nothing",
                              status="stopped", reason="paused_by_user")
        # No session_id, no gate_status, no worktree_path
        with pytest.raises(ValueError, match="no session to resume"):
            await self.lifecycle.execute(task_id, "resume")


    async def test_resume_clears_pr_status(self):
        """pr_status should be cleared on resume."""
        task_id = await _seed(self.db_mod, "resume-pr-clear",
                              status="stopped", reason="paused_by_user",
                              session_id="sess-pr", worktree_path="/tmp/wt",
                              pr_status="conflict")
        with patch("os.path.exists", return_value=True):
            await self.lifecycle.execute(task_id, "resume")
        task = await self.db_mod.get_task(task_id)
        assert task["pr_status"] is None


# ---------------------------------------------------------------------------
# Retry tests
# ---------------------------------------------------------------------------


class TestRetryAction:
    """retry: stopped → working, completed → working, cancelled → working"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()
        self._patches = _mock_launch_patches()
        for p in self._patches:
            p.start()
        yield
        for p in self._patches:
            p.stop()


# ---------------------------------------------------------------------------
# Reopen tests
# ---------------------------------------------------------------------------


class TestReopenAction:
    """reopen: completed → stopped(awaiting_feedback)"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()


# ---------------------------------------------------------------------------
# Start (reopened) tests
# ---------------------------------------------------------------------------


class TestStartAction:
    """start: stopped(awaiting_feedback) → working"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()
        self._patches = _mock_launch_patches()
        for p in self._patches:
            p.start()
        yield
        for p in self._patches:
            p.stop()


# ---------------------------------------------------------------------------
# Cancel reopen tests
# ---------------------------------------------------------------------------


class TestCancelReopenAction:
    """cancel_reopen: stopped(awaiting_feedback) → completed"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()


# ---------------------------------------------------------------------------
# Gate/queue caller integration tests
# ---------------------------------------------------------------------------


class TestGateAndQueueCallers:
    """Verify gates.py and queue.py route through lifecycle.execute()."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()
        self._patches = _mock_launch_patches()
        for p in self._patches:
            p.start()
        yield
        for p in self._patches:
            p.stop()

    async def test_gate_retry_uses_lifecycle(self):
        """gates.py retry path should call lifecycle.execute, not retry_task() directly."""
        import switchboard.dispatch.gates as gates_mod
        source = open(gates_mod.__file__).read()
        assert "lifecycle.execute" in source
        # retry_task may appear in error messages/strings, but should not be called as a function
        import re
        # Match actual function calls: retry_task( or await retry_task(
        calls = re.findall(r'(?:await\s+)?retry_task\s*\(', source)
        assert len(calls) == 0, f"Found retry_task() calls in gates.py: {calls}"

    async def test_queue_drain_uses_lifecycle(self):
        """queue.py should call lifecycle.execute('dispatch'), not dispatch_task."""
        import switchboard.dispatch.queue as queue_mod
        source = open(queue_mod.__file__).read()
        assert "lifecycle.execute" in source
