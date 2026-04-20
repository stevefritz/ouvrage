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

import ouvrage.db as db
from ouvrage.dispatch.lifecycle import (
    IllegalTransition,
    TaskLifecycle,
)


# ---------------------------------------------------------------------------
# Shared helpers and constants
# ---------------------------------------------------------------------------

PROJECT_ID = "action-test-proj"
TASK_PREFIX = "action-test-proj"
_INTERNALS = "ouvrage.dispatch.internals"


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
        patch("ouvrage.notifications.slack.task_dispatched", AsyncMock()),
        patch("ouvrage.notifications.slack.task_attempt_starting", AsyncMock()),
        patch("ouvrage.dispatch.engine.archive_task_logs", AsyncMock()),
        patch("ouvrage.dispatch.engine._invalidate_chain", AsyncMock()),
        patch("ouvrage.dispatch.sdk_session._build_resume_prompt", AsyncMock(return_value="resume prompt")),
        patch("ouvrage.git.operations._sync_branch_with_base", AsyncMock()),
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

    async def test_dispatch_from_ready(self):
        """ready → working, worktree created, SDK launched."""
        task_id = await _seed(self.db_mod, "dispatch-basic")
        result = await self.lifecycle.execute(task_id, "dispatch")
        assert result["status"] == "working"
        task = await self.db_mod.get_task(task_id)
        assert task["status"] == "working"

    async def test_dispatch_increments_dispatch_count(self):
        """dispatch_count should increment."""
        task_id = await _seed(self.db_mod, "dispatch-count")
        await self.lifecycle.execute(task_id, "dispatch")
        task = await self.db_mod.get_task(task_id)
        assert task["dispatch_count"] == 1

    async def test_dispatch_from_working_rejected(self):
        """working → dispatch should raise IllegalTransition."""
        task_id = await _seed(self.db_mod, "dispatch-reject", status="working")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute(task_id, "dispatch")

    async def test_dispatch_queued_when_full(self):
        """When concurrency is full, task reverts to ready with queued_at."""
        # Override check_and_queue_if_full to return True (queued)
        for p in self._patches:
            if hasattr(p, 'attribute') and p.attribute == 'check_and_queue_if_full':
                p.stop()
                break
        with patch(f"{_INTERNALS}.check_and_queue_if_full", AsyncMock(return_value=True)):
            task_id = await _seed(self.db_mod, "dispatch-queued")
            result = await self.lifecycle.execute(task_id, "dispatch")
            # Side effect reverts status to ready with queued_at
            task = await self.db_mod.get_task(task_id)
            assert task["status"] == "ready"
            assert task["queued_at"] is not None

    async def test_dispatch_sets_worktree_path(self):
        """Worktree path should be set after dispatch."""
        task_id = await _seed(self.db_mod, "dispatch-wt")
        await self.lifecycle.execute(task_id, "dispatch")
        task = await self.db_mod.get_task(task_id)
        assert task["worktree_path"] == "/tmp/fake-wt"


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

    async def test_resume_from_stopped_paused(self):
        """stopped(paused) → working with session_id passed to SDK."""
        task_id = await _seed(self.db_mod, "resume-paused",
                              status="stopped", reason="paused_by_user",
                              session_id="sess-123", worktree_path="/tmp/wt")
        # Make os.path.exists return True for worktree
        with patch("os.path.exists", return_value=True):
            result = await self.lifecycle.execute(task_id, "resume")
        assert result["status"] == "working"

    async def test_resume_preserves_gate_status(self):
        """Bug #2 fix: gate_status NOT cleared on resume."""
        task_id = await _seed(self.db_mod, "resume-gate-status",
                              status="stopped", reason="paused_by_user",
                              session_id="sess-456", worktree_path="/tmp/wt",
                              gate_status="test-failed", gate_retries=2)
        with patch("os.path.exists", return_value=True):
            await self.lifecycle.execute(task_id, "resume")
        task = await self.db_mod.get_task(task_id)
        assert task["gate_status"] == "test-failed"
        assert task["gate_retries"] == 2

    async def test_resume_from_cancelled_with_session(self):
        """cancelled → working when session_id exists."""
        task_id = await _seed(self.db_mod, "resume-cancelled",
                              status="cancelled", session_id="sess-789",
                              worktree_path="/tmp/wt")
        with patch("os.path.exists", return_value=True):
            result = await self.lifecycle.execute(task_id, "resume")
        assert result["status"] == "working"

    async def test_resume_from_cancelled_without_session_rejected(self):
        """cancelled without session_id should be rejected."""
        task_id = await _seed(self.db_mod, "resume-no-sess",
                              status="cancelled")
        with pytest.raises(ValueError, match="no session_id"):
            await self.lifecycle.execute(task_id, "resume")

    async def test_resume_from_working_rejected(self):
        """working → resume should raise IllegalTransition."""
        task_id = await _seed(self.db_mod, "resume-working", status="working")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute(task_id, "resume")

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

    async def test_resume_from_stopped_with_gate_resumable(self):
        """stopped with gate_status=testing should be accepted (gate-resumable)."""
        task_id = await _seed(self.db_mod, "resume-gate-resumable",
                              status="stopped", reason="paused_by_user",
                              gate_status="testing", worktree_path="/tmp/wt")
        with patch("os.path.exists", return_value=True):
            result = await self.lifecycle.execute(task_id, "resume")
        assert result["status"] == "working"

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

    async def test_retry_from_stopped(self):
        """stopped → working with fresh session."""
        task_id = await _seed(self.db_mod, "retry-stopped",
                              status="stopped", reason="paused_by_user",
                              worktree_path="/tmp/wt")
        with patch("os.path.exists", return_value=True):
            result = await self.lifecycle.execute(task_id, "retry")
        assert result["status"] == "working"

    async def test_retry_from_completed(self):
        """completed → working."""
        task_id = await _seed(self.db_mod, "retry-completed",
                              status="completed", worktree_path="/tmp/wt")
        with patch("os.path.exists", return_value=True):
            result = await self.lifecycle.execute(task_id, "retry")
        assert result["status"] == "working"

    async def test_retry_from_cancelled(self):
        """cancelled → working."""
        task_id = await _seed(self.db_mod, "retry-cancelled",
                              status="cancelled", worktree_path="/tmp/wt")
        with patch("os.path.exists", return_value=True):
            result = await self.lifecycle.execute(task_id, "retry")
        assert result["status"] == "working"

    async def test_retry_increments_attempt(self):
        """current_attempt should increment."""
        task_id = await _seed(self.db_mod, "retry-attempt",
                              status="stopped", reason="paused_by_user",
                              current_attempt=1, worktree_path="/tmp/wt")
        with patch("os.path.exists", return_value=True):
            await self.lifecycle.execute(task_id, "retry")
        task = await self.db_mod.get_task(task_id)
        assert task["current_attempt"] == 2

    async def test_retry_clears_gate_state(self):
        """session_id, gate_status, gate_passed_at, gate_retries all cleared."""
        task_id = await _seed(self.db_mod, "retry-gate-clear",
                              status="stopped", reason="paused_by_user",
                              session_id="old-sess", gate_status="test-failed",
                              gate_passed_at="2026-01-01", gate_retries=2,
                              worktree_path="/tmp/wt")
        with patch("os.path.exists", return_value=True):
            await self.lifecycle.execute(task_id, "retry")
        task = await self.db_mod.get_task(task_id)
        # session_id preserved for fork-on-retry (no longer cleared)
        assert task["session_id"] == "old-sess"
        assert task["gate_status"] is None
        assert task["gate_passed_at"] is None
        assert task["gate_retries"] == 0

    async def test_retry_posts_attempt_message(self):
        """Should post 'Attempt N starting' message."""
        task_id = await _seed(self.db_mod, "retry-msg",
                              status="stopped", reason="paused_by_user",
                              current_attempt=1, worktree_path="/tmp/wt")
        with patch("os.path.exists", return_value=True):
            await self.lifecycle.execute(task_id, "retry")
        thread = await self.db_mod.read_task_messages(task_id)
        messages = thread.get("messages", [])
        assert any("Attempt 2 starting" in (m.get("title") or "") for m in messages)

    async def test_retry_gate_interrupted_shortcut(self):
        """Task with gate_status=testing re-enters gate pipeline, not CC launch."""
        task_id = await _seed(self.db_mod, "retry-gate-interrupt",
                              status="stopped", reason="paused_by_user",
                              gate_status="testing", worktree_path="/tmp/wt")
        with patch("ouvrage.dispatch.gates._resume_gate_pipeline",
                    AsyncMock()) as mock_gate:
            await self.lifecycle.execute(task_id, "retry")
            mock_gate.assert_called_once_with(task_id, reason="retry")

    async def test_retry_from_working_rejected(self):
        """working → retry should raise IllegalTransition."""
        task_id = await _seed(self.db_mod, "retry-working", status="working")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute(task_id, "retry")

    async def test_retry_from_validating(self):
        """validating → retry (gate auto-retry path)."""
        task_id = await _seed(self.db_mod, "retry-validating",
                              status="pending-validation", gate_status="review-failed",
                              worktree_path="/tmp/wt")
        with patch("os.path.exists", return_value=True):
            result = await self.lifecycle.execute(task_id, "retry")
        assert result["status"] == "working"


# ---------------------------------------------------------------------------
# Reopen tests
# ---------------------------------------------------------------------------


class TestReopenAction:
    """reopen: completed → stopped(awaiting_feedback)"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()

    async def test_reopen_from_completed(self):
        """completed → stopped with reason=awaiting_feedback."""
        task_id = await _seed(self.db_mod, "reopen-basic",
                              status="completed", gate_status="passed",
                              gate_passed_at="2026-01-01T00:00:00Z")
        result = await self.lifecycle.execute(task_id, "reopen")
        assert result["status"] == "stopped"
        assert result["reason"] == "awaiting_feedback"

    async def test_reopen_saves_gate_state(self):
        """Pre-reopen gate_status and gate_passed_at are saved."""
        task_id = await _seed(self.db_mod, "reopen-gate-save",
                              status="completed", gate_status="passed",
                              gate_passed_at="2026-01-01T12:00:00Z")
        await self.lifecycle.execute(task_id, "reopen")
        task = await self.db_mod.get_task(task_id)
        assert task["reopen_saved_gate_status"] == "passed"
        assert task["reopen_saved_gate_passed_at"] == "2026-01-01T12:00:00Z"

    async def test_reopen_increments_attempt(self):
        """current_attempt is incremented for the new revision."""
        task_id = await _seed(self.db_mod, "reopen-attempt",
                              status="completed", current_attempt=1)
        await self.lifecycle.execute(task_id, "reopen")
        task = await self.db_mod.get_task(task_id)
        assert task["current_attempt"] == 2

    async def test_reopen_clears_gate_fields(self):
        """gate_status, gate_passed_at, gate_retries cleared for fresh cycle."""
        task_id = await _seed(self.db_mod, "reopen-clear",
                              status="completed", gate_status="passed",
                              gate_passed_at="2026-01-01", gate_retries=1)
        await self.lifecycle.execute(task_id, "reopen")
        task = await self.db_mod.get_task(task_id)
        assert task["gate_status"] is None
        assert task["gate_passed_at"] is None
        assert task["gate_retries"] == 0

    async def test_reopen_posts_message(self):
        """Should post 'Task reopened — awaiting feedback'."""
        task_id = await _seed(self.db_mod, "reopen-msg", status="completed")
        await self.lifecycle.execute(task_id, "reopen")
        thread = await self.db_mod.read_task_messages(task_id)
        messages = thread.get("messages", [])
        assert any("awaiting feedback" in (m.get("title") or "").lower() for m in messages)

    async def test_reopen_from_stopped_rejected(self):
        """stopped → reopen should raise IllegalTransition."""
        task_id = await _seed(self.db_mod, "reopen-reject", status="stopped")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute(task_id, "reopen")


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

    async def test_start_from_awaiting_feedback(self):
        """stopped(awaiting_feedback) → working."""
        task_id = await _seed(self.db_mod, "start-basic",
                              status="stopped", reason="awaiting_feedback",
                              worktree_path="/tmp/wt", current_attempt=2)
        with patch("os.path.exists", return_value=True):
            result = await self.lifecycle.execute(task_id, "start")
        assert result["status"] == "working"

    async def test_start_from_paused_rejected(self):
        """stopped(paused_by_user) → start should be rejected (wrong reason)."""
        task_id = await _seed(self.db_mod, "start-reject",
                              status="stopped", reason="paused_by_user")
        with pytest.raises(ValueError, match="not awaiting feedback"):
            await self.lifecycle.execute(task_id, "start")

    async def test_start_posts_attempt_message(self):
        """Should post 'Attempt N starting' message."""
        task_id = await _seed(self.db_mod, "start-msg",
                              status="stopped", reason="awaiting_feedback",
                              current_attempt=3, worktree_path="/tmp/wt")
        with patch("os.path.exists", return_value=True):
            await self.lifecycle.execute(task_id, "start")
        thread = await self.db_mod.read_task_messages(task_id)
        messages = thread.get("messages", [])
        assert any("Attempt 3 starting" in (m.get("title") or "") for m in messages)

    async def test_start_collects_reopen_feedback(self):
        """Should call collect_reopen_feedback."""
        task_id = await _seed(self.db_mod, "start-feedback",
                              status="stopped", reason="awaiting_feedback",
                              worktree_path="/tmp/wt", current_attempt=2)
        with patch("os.path.exists", return_value=True), \
             patch(f"{_INTERNALS}.collect_reopen_feedback",
                    AsyncMock(return_value="feedback")) as mock_fb:
            await self.lifecycle.execute(task_id, "start")
            mock_fb.assert_called_once()


# ---------------------------------------------------------------------------
# Cancel reopen tests
# ---------------------------------------------------------------------------


class TestCancelReopenAction:
    """cancel_reopen: stopped(awaiting_feedback) → completed"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()

    async def test_cancel_reopen_restores_state(self):
        """Gate state should be restored from saved values."""
        task_id = await _seed(self.db_mod, "cancel-reopen-restore",
                              status="stopped", reason="awaiting_feedback",
                              current_attempt=2,
                              reopen_saved_gate_status="passed",
                              reopen_saved_gate_passed_at="2026-01-01T00:00:00Z")
        result = await self.lifecycle.execute(task_id, "cancel_reopen")
        assert result["status"] == "completed"
        task = await self.db_mod.get_task(task_id)
        assert task["gate_status"] == "passed"
        assert task["gate_passed_at"] == "2026-01-01T00:00:00Z"
        assert task["reopen_saved_gate_status"] is None
        assert task["reopen_saved_gate_passed_at"] is None

    async def test_cancel_reopen_decrements_attempt(self):
        """current_attempt should decrement back."""
        task_id = await _seed(self.db_mod, "cancel-reopen-attempt",
                              status="stopped", reason="awaiting_feedback",
                              current_attempt=3)
        await self.lifecycle.execute(task_id, "cancel_reopen")
        task = await self.db_mod.get_task(task_id)
        assert task["current_attempt"] == 2

    async def test_cancel_reopen_from_paused_rejected(self):
        """stopped(paused_by_user) → cancel_reopen should be rejected."""
        task_id = await _seed(self.db_mod, "cancel-reopen-reject",
                              status="stopped", reason="paused_by_user")
        with pytest.raises(ValueError, match="not awaiting feedback"):
            await self.lifecycle.execute(task_id, "cancel_reopen")


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
        import ouvrage.dispatch.gates as gates_mod
        source = open(gates_mod.__file__).read()
        assert "lifecycle.execute" in source
        # retry_task may appear in error messages/strings, but should not be called as a function
        import re
        # Match actual function calls: retry_task( or await retry_task(
        calls = re.findall(r'(?:await\s+)?retry_task\s*\(', source)
        assert len(calls) == 0, f"Found retry_task() calls in gates.py: {calls}"

    async def test_queue_drain_uses_lifecycle(self):
        """queue.py should call lifecycle.execute('dispatch'), not dispatch_task."""
        import ouvrage.dispatch.queue as queue_mod
        source = open(queue_mod.__file__).read()
        assert "lifecycle.execute" in source
