"""Tests for crash recovery: auto-resume, retry, gate re-trigger, stagger, flap detection."""

import asyncio
import os
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from switchboard.dispatch.recovery import (
    _classify_orphan,
    _classify_with_dependents,
    _verify_worktree,
    recover_orphaned_tasks,
    _recover_task,
    _recover_single_task,
    _recover_with_resume,
    _recover_with_retry,
)
from switchboard.config.settings import (
    RECOVERY_STAGGER_SECONDS,
    MAX_RECOVERY_ATTEMPTS,
    RECOVERY_ENABLED,
)
from switchboard.config.constants import STALL_THRESHOLD_SECONDS
from switchboard.dispatch._state import _active_clients
from switchboard.dispatch.engine import resume_task, retry_task
import switchboard.db as _db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _create_orphan(db, task_id="test-project/orphan-1", session_id="sess-abc",
                          parent_task_id=None, worktree_path="/tmp/fake-worktree",
                          recovery_count=0, phase="implementing", **kwargs):
    """Create a task stuck in 'working' with no live PID (orphaned)."""
    task = await db.create_task(
        id=task_id,
        project_id="test-project",
        goal="Test orphaned task",
        parent_task_id=parent_task_id,
        **kwargs,
    )
    await db.update_task(task_id,
                         status="working",
                         session_id=session_id,
                         worktree_path=worktree_path,
                         phase=phase,
                         recovery_count=recovery_count,
                         pid=99999)  # dead PID
    return await db.get_task(task_id)


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------

class TestClassifyOrphan:
    """_classify_orphan returns correct priority and method."""

    def test_gate_subtask_priority(self):
        priority, method = _classify_orphan({"parent_task_id": "parent-1", "session_id": "s1"})
        assert priority == 0
        assert method == "gate_subtask"

    def test_resumable_priority(self):
        priority, method = _classify_orphan({"session_id": "s1"})
        assert priority == 2
        assert method == "resume"

    def test_retryable_priority(self):
        priority, method = _classify_orphan({})
        assert priority == 3
        assert method == "retry"

    async def test_chain_parent_upgraded(self, db, sample_project):
        # Create parent + dependent
        parent = await _create_orphan(db, "test-project/parent", session_id=None)
        await db.create_task(id="test-project/child", project_id="test-project",
                             goal="child", depends_on="test-project/parent")
        priority, method = await _classify_with_dependents(parent)
        assert priority == 1  # upgraded from 3 to 1
        assert method == "retry"


# ---------------------------------------------------------------------------
# Recovery: resume path
# ---------------------------------------------------------------------------

class TestRecoverWithResume:
    """Tasks with session_id get resumed."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_setup_worktree = AsyncMock(return_value="/tmp/fake-worktree")
        self.mock_run_setup = AsyncMock()
        self.mock_run_sdk = AsyncMock()
        self.mock_verify = patch("switchboard.dispatch.recovery._verify_worktree", AsyncMock(return_value=True))

        patches = [
            patch("switchboard.dispatch.engine.setup_worktree", self.mock_setup_worktree),
            patch("switchboard.dispatch.engine.run_setup_command", self.mock_run_setup),
            patch("switchboard.dispatch.engine._run_sdk_session", self.mock_run_sdk),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
            self.mock_verify,
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_resume_with_session_id(self, db, sample_project):
        """Task with session_id gets resumed via resume_task."""
        orphan = await _create_orphan(db, session_id="sess-123")

        # Run recovery
        await recover_orphaned_tasks()

        # Task should be working (dispatched)
        task = await db.get_task("test-project/orphan-1")
        assert task["status"] == "working"
        assert task["recovery_count"] == 1
        assert task["last_recovery_at"] is not None

    async def test_resume_fallback_to_retry_on_bad_worktree(self, db, sample_project):
        """If worktree is missing, falls back to retry."""
        # Override worktree check to return False
        with patch("switchboard.dispatch.recovery._verify_worktree", AsyncMock(return_value=False)):
            orphan = await _create_orphan(db, session_id="sess-123")
            await recover_orphaned_tasks()

        task = await db.get_task("test-project/orphan-1")
        assert task["status"] == "working"
        # session_id should be cleared (retry clears it)
        assert task["session_id"] is None


# ---------------------------------------------------------------------------
# Recovery: retry path
# ---------------------------------------------------------------------------

class TestRecoverWithRetry:
    """Tasks without session_id get fresh retry."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        patches = [
            patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake-worktree")),
            patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()),
            patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_retry_without_session(self, db, sample_project):
        """Task without session_id gets fresh retry."""
        orphan = await _create_orphan(db, session_id=None)
        await recover_orphaned_tasks()

        task = await db.get_task("test-project/orphan-1")
        assert task["status"] == "working"
        assert task["recovery_count"] == 1


# ---------------------------------------------------------------------------
# Recovery: gate subtask
# ---------------------------------------------------------------------------

class TestRecoverGateSubtask:
    """Gate subtask recovery re-triggers parent gate."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_run_test_gate = AsyncMock()
        self.mock_dispatch_review = AsyncMock()

        patches = [
            patch("switchboard.dispatch.gates._run_test_gate", self.mock_run_test_gate),
            patch("switchboard.dispatch.gates._dispatch_review", self.mock_dispatch_review),
            patch("switchboard.dispatch.gates._run_as_worker", AsyncMock(return_value=(b"", b"", 0))),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
            # Worktree existence guards — these tests cover gate subtask routing,
            # not worktree existence, so bypass both guard points.
            patch("switchboard.dispatch.gates._verify_worktree_exists", AsyncMock(return_value=True)),
            patch("switchboard.dispatch.recovery.os.path.exists", return_value=True),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_gate_subtask_retriggers_test(self, db, sample_project):
        """Orphaned review/test subtask re-triggers parent gate pipeline."""
        # Create parent in testing state
        parent = await db.create_task(id="test-project/parent", project_id="test-project",
                                       goal="Parent task")
        await db.update_task("test-project/parent", status="completed",
                             gate_status="testing", auto_test=True)

        # Create orphaned subtask
        await _create_orphan(db, task_id="test-project/review-sub",
                             parent_task_id="test-project/parent",
                             session_id="sess-review")

        await recover_orphaned_tasks()

        # Subtask should be cancelled
        sub = await db.get_task("test-project/review-sub")
        assert sub["status"] == "cancelled"

        # Parent gate should be re-triggered — both the unified gate sweep and
        # _recover_gate_subtask trigger it; duplicate guard ensures only one runs.
        self.mock_run_test_gate.assert_called()

    async def test_gate_subtask_retriggers_review(self, db, sample_project):
        """Orphaned subtask with reviewing parent re-triggers review."""
        parent = await db.create_task(id="test-project/parent-rev", project_id="test-project",
                                       goal="Parent task review")
        await db.update_task("test-project/parent-rev", status="completed",
                             gate_status="reviewing", auto_review=True)

        await _create_orphan(db, task_id="test-project/review-sub-2",
                             parent_task_id="test-project/parent-rev",
                             session_id="sess-review-2")

        await recover_orphaned_tasks()

        # _dispatch_review is triggered from both unified sweep and _recover_gate_subtask
        self.mock_dispatch_review.assert_called()


# ---------------------------------------------------------------------------
# Stagger timing
# ---------------------------------------------------------------------------

class TestStaggerRecovery:
    """Stagger delays between task recoveries."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        patches = [
            patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake-worktree")),
            patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()),
            patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
            patch("switchboard.dispatch.recovery._verify_worktree", AsyncMock(return_value=True)),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_stagger_sleep_called(self, db, sample_project):
        """Second and subsequent tasks get asyncio.sleep stagger."""
        await _create_orphan(db, "test-project/orphan-a", session_id="s1")
        await _create_orphan(db, "test-project/orphan-b", session_id="s2")

        sleep_calls = []
        original_sleep = asyncio.sleep

        async def mock_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("switchboard.dispatch.recovery.asyncio.sleep", side_effect=mock_sleep):
            await recover_orphaned_tasks()

        # First task: no sleep. Second: sleep with stagger delay.
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == RECOVERY_STAGGER_SECONDS


# ---------------------------------------------------------------------------
# Flap detection
# ---------------------------------------------------------------------------

class TestFlapDetection:
    """Tasks that have been recovered too many times get escalated."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        patches = [
            patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake-worktree")),
            patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()),
            patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_flap_detection_escalates(self, db, sample_project):
        """Task with recovery_count >= MAX_RECOVERY_ATTEMPTS goes to needs-review."""
        await _create_orphan(db, recovery_count=3)  # Already at max (default 3)

        await recover_orphaned_tasks()

        task = await db.get_task("test-project/orphan-1")
        assert task["status"] == "needs-review"

        # Check message was posted
        thread = await db.read_task_messages("test-project/orphan-1")
        messages = thread.get("messages", [])
        assert any("Recovery limit reached" in m.get("title", "") for m in messages)

    async def test_at_boundary_escalates(self, db, sample_project):
        """Task with recovery_count exactly at MAX_RECOVERY_ATTEMPTS escalates (no off-by-one)."""
        # recovery_count=3 in DB, MAX=3 → should escalate (3 >= 3), NOT recover
        await _create_orphan(db, recovery_count=3)

        await recover_orphaned_tasks()

        task = await db.get_task("test-project/orphan-1")
        assert task["status"] == "needs-review"
        # Count should NOT be incremented — we checked before incrementing
        assert task["recovery_count"] == 3

    async def test_one_below_boundary_recovers(self, db, sample_project):
        """Task with recovery_count one below MAX still recovers."""
        # recovery_count=2 in DB, MAX=3 → should recover (2 < 3), count becomes 3
        await _create_orphan(db, recovery_count=2, session_id=None)

        await recover_orphaned_tasks()

        task = await db.get_task("test-project/orphan-1")
        assert task["status"] == "working"
        assert task["recovery_count"] == 3

    async def test_under_flap_limit_recovers(self, db, sample_project):
        """Task with recovery_count < MAX_RECOVERY_ATTEMPTS recovers normally."""
        await _create_orphan(db, recovery_count=1, session_id=None)

        await recover_orphaned_tasks()

        task = await db.get_task("test-project/orphan-1")
        assert task["status"] == "working"
        assert task["recovery_count"] == 2


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------

class TestRecoveryPriority:
    """Recovery dispatches in correct priority order."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        patches = [
            patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake-worktree")),
            patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()),
            patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
            patch("switchboard.dispatch.recovery._verify_worktree", AsyncMock(return_value=True)),
            patch("switchboard.dispatch.gates._run_test_gate", AsyncMock()),
            patch("switchboard.dispatch.gates._dispatch_review", AsyncMock()),
            patch("switchboard.dispatch.gates._run_as_worker", AsyncMock(return_value=(b"", b"", 0))),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_priority_order(self, db, sample_project):
        """Gate subtasks before chain parents before resumable before retryable."""
        recovery_order = []
        original_recover = _recover_task

        async def tracking_recover(task_id, task, method):
            recovery_order.append((task_id, method))
            if method != "gate_subtask":
                await original_recover(task_id, task, method)

        # Create parent for gate subtask
        parent = await db.create_task(id="test-project/gate-parent", project_id="test-project",
                                       goal="Gate parent")
        await db.update_task("test-project/gate-parent", status="completed",
                             gate_status="testing", auto_test=True)

        # Create in reverse priority order
        await _create_orphan(db, "test-project/retryable", session_id=None)  # prio 3
        await _create_orphan(db, "test-project/resumable", session_id="s1")  # prio 2
        await _create_orphan(db, "test-project/gate-sub", session_id="s2",
                             parent_task_id="test-project/gate-parent")  # prio 0

        with patch("switchboard.dispatch.recovery._recover_task", side_effect=tracking_recover), \
             patch("switchboard.dispatch.recovery.asyncio.sleep", AsyncMock()):
            await recover_orphaned_tasks()

        # Gate subtask first, then resumable, then retryable
        assert recovery_order[0] == ("test-project/gate-sub", "gate_subtask")
        assert recovery_order[1] == ("test-project/resumable", "resume")
        assert recovery_order[2] == ("test-project/retryable", "retry")


# ---------------------------------------------------------------------------
# RECOVERY_ENABLED=false
# ---------------------------------------------------------------------------

class TestRecoveryDisabled:
    """RECOVERY_ENABLED=false skips auto-recovery."""

    async def test_disabled_marks_needs_review(self, db, sample_project):
        """When disabled, orphans are just marked needs-review (old behavior)."""
        await _create_orphan(db)

        with patch("switchboard.dispatch.recovery.RECOVERY_ENABLED", False), \
             patch("switchboard.dispatch.engine.notify", AsyncMock()):
            await recover_orphaned_tasks()

        task = await db.get_task("test-project/orphan-1")
        assert task["status"] == "needs-review"


# ---------------------------------------------------------------------------
# Concurrency limit during recovery
# ---------------------------------------------------------------------------

class TestConcurrencyDuringRecovery:
    """Recovery respects concurrency limits and queues excess tasks."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        patches = [
            patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake-worktree")),
            patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()),
            patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
            patch("switchboard.dispatch.recovery._verify_worktree", AsyncMock(return_value=True)),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_queued_with_recovery_priority(self, db, sample_project):
        """When concurrency is full, recovery tasks queue with priority flag."""
        # Fill all concurrency slots with non-orphan working tasks (live PID)
        max_concurrent = _db.DEFAULT_MAX_CONCURRENT
        for i in range(max_concurrent):
            t = await db.create_task(id=f"test-project/active-{i}",
                                      project_id="test-project", goal=f"Active {i}")
            await db.update_task(t["id"], status="working", pid=os.getpid())  # live PID

        # Create orphan (dead PID)
        await _create_orphan(db, session_id="s1")

        await recover_orphaned_tasks()

        task = await db.get_task("test-project/orphan-1")
        assert task["status"] == "ready"
        assert task["queued_at"] is not None
        assert task["recovery_priority"] == 1  # True


# ---------------------------------------------------------------------------
# FIFO queue integration: recovery_priority
# ---------------------------------------------------------------------------

class TestRecoveryQueuePriority:
    """Recovery tasks get dispatched before regular queued tasks."""

    async def test_recovery_priority_front_of_queue(self, db, sample_project):
        """get_queued_tasks returns recovery-priority tasks before regular ones."""
        # Create a regular queued task
        t1 = await db.create_task(id="test-project/regular-q", project_id="test-project",
                                   goal="Regular queued")
        await db.update_task("test-project/regular-q", queued_at="2026-01-01T00:00:00Z")

        # Create a recovery-priority task queued AFTER the regular one
        t2 = await db.create_task(id="test-project/recovery-q", project_id="test-project",
                                   goal="Recovery queued")
        await db.update_task("test-project/recovery-q",
                             queued_at="2026-01-01T00:01:00Z", recovery_priority=True)

        queued = await db.get_queued_tasks()
        assert len(queued) == 2
        assert queued[0]["id"] == "test-project/recovery-q"
        assert queued[1]["id"] == "test-project/regular-q"


# ---------------------------------------------------------------------------
# Worktree verification
# ---------------------------------------------------------------------------

class TestWorktreeVerification:
    """_verify_worktree checks worktree existence and cleanliness."""

    async def test_missing_worktree(self):
        assert await _verify_worktree({"worktree_path": "/nonexistent/path"}) is False

    async def test_no_worktree_path(self):
        assert await _verify_worktree({}) is False
        assert await _verify_worktree({"worktree_path": None}) is False

    async def test_valid_clean_worktree(self, tmp_path):
        """Worktree with .git and clean git status passes verification."""
        (tmp_path / ".git").touch()  # worktrees have a .git file, not dir

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("switchboard.dispatch.recovery.asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            assert await _verify_worktree({"worktree_path": str(tmp_path)}) is True

    async def test_dirty_worktree_passes(self, tmp_path):
        """Dirty worktree is OK for resume — SIGTERM'd tasks always have uncommitted changes."""
        (tmp_path / ".git").touch()

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b" M tasks.py\n", b""))

        with patch("switchboard.dispatch.recovery.asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            assert await _verify_worktree({"worktree_path": str(tmp_path)}) is True

    async def test_corrupted_worktree_fails(self, tmp_path):
        """Worktree where git status returns non-zero fails verification."""
        (tmp_path / ".git").touch()

        mock_proc = AsyncMock()
        mock_proc.returncode = 128
        mock_proc.communicate = AsyncMock(return_value=(b"", b"fatal: not a git repository"))

        with patch("switchboard.dispatch.recovery.asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            assert await _verify_worktree({"worktree_path": str(tmp_path)}) is False


# ---------------------------------------------------------------------------
# Resume failure fallback
# ---------------------------------------------------------------------------

class TestResumeFailureFallback:
    """If resume fails, falls back to retry."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        patches = [
            patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake-worktree")),
            patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()),
            patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
            patch("switchboard.dispatch.recovery._verify_worktree", AsyncMock(return_value=True)),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_resume_fail_triggers_retry(self, db, sample_project):
        """When resume raises, recovery falls back to retry."""
        orphan = await _create_orphan(db, session_id="sess-expired")

        call_count = {"resume": 0, "retry": 0}
        original_retry = retry_task

        async def failing_resume(task_id, reset_recovery_count=True):
            call_count["resume"] += 1
            raise RuntimeError("Session expired")

        async def tracking_retry(task_id, clean=False):
            call_count["retry"] += 1
            return await original_retry(task_id, clean=clean)

        with patch("switchboard.dispatch.engine.resume_task", side_effect=failing_resume), \
             patch("switchboard.dispatch.engine.retry_task", side_effect=tracking_retry):
            await recover_orphaned_tasks()

        assert call_count["resume"] == 1
        assert call_count["retry"] == 1

        task = await db.get_task("test-project/orphan-1")
        assert task["status"] == "working"


# ---------------------------------------------------------------------------
# Status messages
# ---------------------------------------------------------------------------

class TestRecoveryStatusMessages:
    """Every recovered task gets a descriptive status message."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        patches = [
            patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake-worktree")),
            patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()),
            patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
            patch("switchboard.dispatch.recovery._verify_worktree", AsyncMock(return_value=True)),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_recovery_message_posted(self, db, sample_project):
        """Recovery posts a status message with details including checklist progress."""
        await _create_orphan(db, session_id="sess-abc", phase="implementing")

        # Add checklist items so the message includes progress
        await db.create_checklist_items("test-project/orphan-1", [
            "Step one", "Step two", "Step three",
        ])
        items = await db.get_checklist("test-project/orphan-1")
        await db.update_checklist_item(items[0]["id"], done=True)

        await recover_orphaned_tasks()

        thread = await db.read_task_messages("test-project/orphan-1")
        messages = thread.get("messages", [])
        recovery_msgs = [m for m in messages if "auto-recovery" in m.get("title", "").lower()
                         or "auto-recover" in m.get("title", "").lower()]
        assert len(recovery_msgs) >= 1
        msg = recovery_msgs[0]["content"]
        assert "Service restart" in msg
        assert "implementing" in msg
        assert "sess-abc" in msg
        # Should include checklist progress (1/3)
        assert "1/3 checklist" in msg



# ---------------------------------------------------------------------------
# _recover_single_task — health-check recovery (Fix 2 + Fix 4)
# ---------------------------------------------------------------------------

class TestRecoverSingleTask:
    """_recover_single_task sets status=needs-review before calling resume/retry (Fix 2).

    Before Fix 2, resume_task/retry_task were called while status='working',
    causing them to reject the task. The exception handler then set needs-review,
    so recovery only worked on the SECOND health check cycle.

    After Fix 2, status is set to needs-review atomically in the same update_task
    call as recovery_count increment, so resume/retry always succeed on first attempt.
    """

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        patches = [
            patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake-worktree")),
            patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()),
            patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
            patch("switchboard.dispatch.recovery._verify_worktree", AsyncMock(return_value=True)),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_status_set_to_needs_review_before_resume(self, db, sample_project):
        """Fix 2: task status is needs-review when resume_task is called."""
        task = await _create_orphan(db, session_id="sess-xyz")

        status_at_resume = {}

        async def capturing_resume(task_id):
            t = await db.get_task(task_id)
            status_at_resume["status"] = t["status"]
            # Call real resume (patches are in place for dispatch)
            await _recover_with_resume(task_id, t)

        with patch("switchboard.dispatch.engine.resume_task", side_effect=capturing_resume):
            await _recover_single_task(task)

        assert status_at_resume.get("status") == "needs-review", (
            "Task must be in needs-review status when resume_task is called, "
            "not 'working' — otherwise resume_task will reject it"
        )

    async def test_status_set_to_needs_review_before_retry(self, db, sample_project):
        """Fix 2: task status is needs-review when retry_task is called."""
        task = await _create_orphan(db, session_id=None)

        status_at_retry = {}

        async def capturing_retry(task_id, clean=False):
            t = await db.get_task(task_id)
            status_at_retry["status"] = t["status"]
            await _recover_with_retry(task_id, t)

        with patch("switchboard.dispatch.engine.retry_task", side_effect=capturing_retry):
            await _recover_single_task(task)

        assert status_at_retry.get("status") == "needs-review", (
            "Task must be in needs-review status when retry_task is called"
        )

    async def test_recovery_count_incremented(self, db, sample_project):
        """_recover_single_task increments recovery_count before calling resume/retry."""
        task = await _create_orphan(db, session_id=None, recovery_count=1)

        resume_called = []

        async def capturing_retry(task_id, clean=False):
            t = await db.get_task(task_id)
            resume_called.append(t.get("recovery_count"))

        with patch("switchboard.dispatch.engine.retry_task", side_effect=capturing_retry):
            await _recover_single_task(task)

        assert resume_called == [2]

    async def test_working_task_recovered_on_first_health_check_cycle(self, db, sample_project):
        """Fix 4: a task left 'working' by SIGTERM is correctly recovered on first cycle.

        This verifies the SIGTERM recovery interaction: with Fix 2 in place, the
        health check no longer needs two cycles to recover (first sets needs-review
        via exception, second actually resumes).
        """
        # Simulate a task left in 'working' state by SIGTERM
        task = await _create_orphan(db, session_id="sess-sigterm")

        dispatch_calls = []

        async def capturing_resume(task_id):
            dispatch_calls.append(task_id)
            # Simulate successful dispatch by setting status=working
            await db.update_task(task_id, status="working")

        with patch("switchboard.dispatch.engine.resume_task", side_effect=capturing_resume):
            await _recover_single_task(task)

        # Must have been called on this first invocation — not deferred
        assert dispatch_calls == [task["id"]], (
            "resume_task must be called on the first _recover_single_task invocation"
        )

    async def test_flap_detection_in_single_recover(self, db, sample_project):
        """_recover_single_task respects MAX_RECOVERY_ATTEMPTS."""
        task = await _create_orphan(db, recovery_count=MAX_RECOVERY_ATTEMPTS)

        resume_called = []
        with patch("switchboard.dispatch.engine.resume_task", side_effect=lambda _: resume_called.append(True)):
            await _recover_single_task(task)

        assert resume_called == [], "resume_task must not be called when flap limit reached"
        result = await db.get_task(task["id"])
        assert result["status"] == "needs-review"
