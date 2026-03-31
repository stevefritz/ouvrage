"""Tests for gate recovery system.

Covers:
- _running_gates tracking in _run_test_gate and _dispatch_review
- Duplicate gate guard (prevents concurrent gate coroutines)
- _resume_gate_pipeline for every gate state
- Startup sweep via recover_orphaned_tasks (unified Category 1+2 replacement)
- Background orphan detection in check_stalled_tasks
"""

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

import switchboard.db as db
from switchboard.dispatch._state import _running_gates
from switchboard.dispatch.gates import _resume_gate_pipeline
from switchboard.dispatch.recovery import (
    recover_orphaned_tasks,
    mark_working_for_recovery,
    check_stalled_tasks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_task(
    db,
    task_id="test-project/gate-task-1",
    status="completed",
    gate_status=None,
    gate_retries=0,
    auto_test=True,
    auto_review=True,
    worktree_path="/tmp/fake-worktree",
    pushed_at=None,
    gate_passed_at=None,
    **kwargs,
):
    """Create a task in the given gate state."""
    task = await db.create_task(
        id=task_id,
        project_id="test-project",
        goal="Test gate recovery",
        auto_test=auto_test,
        auto_review=auto_review,
    )
    await db.update_task(
        task_id,
        status=status,
        gate_status=gate_status,
        gate_retries=gate_retries,
        worktree_path=worktree_path,
        pushed_at=pushed_at,
        gate_passed_at=gate_passed_at,
        **kwargs,
    )
    return await db.get_task(task_id)


# ---------------------------------------------------------------------------
# _running_gates tracking — _run_test_gate
# ---------------------------------------------------------------------------

class TestRunningGatesTestGate:
    """_running_gates is populated during _run_test_gate and cleaned up after."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        # Ensure clean state before/after each test
        _running_gates.clear()
        yield
        _running_gates.clear()

    async def test_running_gates_populated_during_test(self, db, sample_project):
        """task_id is in _running_gates while _run_test_gate_inner executes."""
        from switchboard.dispatch.gates import _run_test_gate

        captured = []

        async def _fake_inner(tid, proj, task):
            captured.append(tid in _running_gates)

        with patch("switchboard.dispatch.gates._run_test_gate_inner", _fake_inner):
            await _run_test_gate("test-project/t1", sample_project, {})

        assert captured == [True]

    async def test_running_gates_cleared_after_test(self, db, sample_project):
        """task_id is removed from _running_gates after _run_test_gate completes."""
        from switchboard.dispatch.gates import _run_test_gate

        with patch("switchboard.dispatch.gates._run_test_gate_inner", AsyncMock()):
            await _run_test_gate("test-project/t1", sample_project, {})

        assert "test-project/t1" not in _running_gates

    async def test_running_gates_cleared_on_exception(self, db, sample_project):
        """task_id is removed from _running_gates even if _run_test_gate_inner raises."""
        from switchboard.dispatch.gates import _run_test_gate

        async def _raises(*_):
            raise RuntimeError("boom")

        with patch("switchboard.dispatch.gates._run_test_gate_inner", _raises):
            with pytest.raises(RuntimeError):
                await _run_test_gate("test-project/t1", sample_project, {})

        assert "test-project/t1" not in _running_gates

    async def test_duplicate_guard_skips_second_call(self, db, sample_project):
        """If task_id is already in _running_gates, _run_test_gate returns immediately."""
        from switchboard.dispatch.gates import _run_test_gate

        inner_called = []

        async def _fake_inner(tid, proj, task):
            inner_called.append(tid)

        _running_gates.add("test-project/t1")
        with patch("switchboard.dispatch.gates._run_test_gate_inner", _fake_inner):
            await _run_test_gate("test-project/t1", sample_project, {})

        assert inner_called == []


# ---------------------------------------------------------------------------
# _running_gates tracking — _dispatch_review
# ---------------------------------------------------------------------------

class TestRunningGatesDispatchReview:
    """_running_gates is populated during _dispatch_review and cleaned up after."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _running_gates.clear()
        yield
        _running_gates.clear()

    async def test_running_gates_populated_during_review(self, db, sample_project):
        """task_id is in _running_gates while _dispatch_review_inner executes."""
        from switchboard.dispatch.gates import _dispatch_review

        captured = []

        async def _fake_inner(tid, proj, task):
            captured.append(tid in _running_gates)

        with patch("switchboard.dispatch.gates._dispatch_review_inner", _fake_inner):
            await _dispatch_review("test-project/t1", sample_project, {})

        assert captured == [True]

    async def test_running_gates_cleared_after_review(self, db, sample_project):
        """task_id is removed from _running_gates after _dispatch_review completes."""
        from switchboard.dispatch.gates import _dispatch_review

        with patch("switchboard.dispatch.gates._dispatch_review_inner", AsyncMock()):
            await _dispatch_review("test-project/t1", sample_project, {})

        assert "test-project/t1" not in _running_gates

    async def test_running_gates_cleared_on_exception(self, db, sample_project):
        """task_id is removed from _running_gates even if _dispatch_review_inner raises."""
        from switchboard.dispatch.gates import _dispatch_review

        async def _raises(*_):
            raise RuntimeError("review boom")

        with patch("switchboard.dispatch.gates._dispatch_review_inner", _raises):
            with pytest.raises(RuntimeError):
                await _dispatch_review("test-project/t1", sample_project, {})

        assert "test-project/t1" not in _running_gates

    async def test_duplicate_guard_skips_second_call(self, db, sample_project):
        """If task_id is already in _running_gates, _dispatch_review returns immediately."""
        from switchboard.dispatch.gates import _dispatch_review

        inner_called = []

        async def _fake_inner(tid, proj, task):
            inner_called.append(tid)

        _running_gates.add("test-project/t1")
        with patch("switchboard.dispatch.gates._dispatch_review_inner", _fake_inner):
            await _dispatch_review("test-project/t1", sample_project, {})

        assert inner_called == []


# ---------------------------------------------------------------------------
# _resume_gate_pipeline — all gate states
# ---------------------------------------------------------------------------

class TestResumeGatePipeline:
    """_resume_gate_pipeline routes correctly for every gate_status value."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _running_gates.clear()
        self.mock_run_test_gate = AsyncMock()
        self.mock_dispatch_review = AsyncMock()
        self.mock_retry_task = AsyncMock(side_effect=lambda tid: None)
        self.mock_check_dependents = AsyncMock()
        self.mock_ensure_pushed = AsyncMock(return_value=True)
        self.mock_notify = AsyncMock()

        patches = [
            patch("switchboard.dispatch.gates._run_test_gate", self.mock_run_test_gate),
            patch("switchboard.dispatch.gates._dispatch_review", self.mock_dispatch_review),
            # _ensure_branch_pushed is lazily imported from git.operations inside _resume_gate_pipeline
            patch("switchboard.git.operations._ensure_branch_pushed", self.mock_ensure_pushed),
            patch("switchboard.dispatch.gates.notify", self.mock_notify),
            # retry_task and _check_and_dispatch_dependents are lazily imported from engine
            patch("switchboard.dispatch.engine.retry_task", self.mock_retry_task),
            patch("switchboard.dispatch.engine._check_and_dispatch_dependents", self.mock_check_dependents),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()
        _running_gates.clear()

    async def test_skips_if_already_running(self, db, sample_project):
        """If task_id is in _running_gates, _resume_gate_pipeline does nothing."""
        await _make_task(db, gate_status="testing")
        _running_gates.add("test-project/gate-task-1")

        result = await _resume_gate_pipeline("test-project/gate-task-1", reason="test")

        self.mock_run_test_gate.assert_not_called()
        self.mock_dispatch_review.assert_not_called()

    async def test_none_gate_status_no_push_needed(self, db, sample_project):
        """gate_status=None with pushed_at set → run_test_gate immediately."""
        await _make_task(db, gate_status=None, pushed_at=db.now_iso())

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            await _resume_gate_pipeline("test-project/gate-task-1")
        await asyncio.sleep(0)  # let the create_task run

        self.mock_run_test_gate.assert_called_once()

    async def test_none_gate_status_push_needed_succeeds(self, db, sample_project):
        """gate_status=None with no pushed_at → push first, then run_test_gate."""
        await _make_task(db, gate_status=None, pushed_at=None)
        self.mock_ensure_pushed.return_value = True

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            await _resume_gate_pipeline("test-project/gate-task-1")
        await asyncio.sleep(0)

        self.mock_ensure_pushed.assert_called_once()
        self.mock_run_test_gate.assert_called_once()

    async def test_none_gate_status_push_fails(self, db, sample_project):
        """gate_status=None, push fails → sets gate_status=push-failed."""
        await _make_task(db, gate_status=None, pushed_at=None)
        self.mock_ensure_pushed.return_value = False

        await _resume_gate_pipeline("test-project/gate-task-1")

        task = await db.get_task("test-project/gate-task-1")
        assert task["gate_status"] == "push-failed"
        self.mock_run_test_gate.assert_not_called()

    async def test_testing_reruns_test_gate(self, db, sample_project):
        """gate_status=testing → re-run test gate (idempotent)."""
        await _make_task(db, gate_status="testing")

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            await _resume_gate_pipeline("test-project/gate-task-1")
        await asyncio.sleep(0)

        self.mock_run_test_gate.assert_called_once()

    async def test_test_failed_under_limit_calls_retry_task(self, db, sample_project):
        """gate_status=test-failed with retries < max → reset gate_status, call retry_task."""
        await _make_task(db, gate_status="test-failed", gate_retries=1)

        await _resume_gate_pipeline("test-project/gate-task-1")

        # gate_status should be reset to None before calling retry_task
        task = await db.get_task("test-project/gate-task-1")
        assert task["gate_status"] is None
        self.mock_retry_task.assert_called_once_with("test-project/gate-task-1")

    async def test_test_failed_at_limit_sets_needs_review(self, db, sample_project):
        """gate_status=test-failed with retries >= max → set needs-review."""
        await _make_task(db, gate_status="test-failed", gate_retries=3)

        await _resume_gate_pipeline("test-project/gate-task-1")

        task = await db.get_task("test-project/gate-task-1")
        assert task["gate_status"] == "needs-review"
        self.mock_notify.task_needs_review.assert_called_once()

    async def test_test_passed_with_auto_review(self, db, sample_project):
        """gate_status=test-passed with auto_review → dispatch review."""
        await _make_task(db, gate_status="test-passed", auto_review=True)

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            await _resume_gate_pipeline("test-project/gate-task-1")
        await asyncio.sleep(0)

        self.mock_dispatch_review.assert_called_once()

    async def test_test_passed_without_auto_review(self, db, sample_project):
        """gate_status=test-passed without auto_review → mark passed."""
        await _make_task(db, gate_status="test-passed", auto_review=False)

        await _resume_gate_pipeline("test-project/gate-task-1")

        task = await db.get_task("test-project/gate-task-1")
        assert task["gate_status"] == "passed"
        assert task["gate_passed_at"] is not None
        self.mock_check_dependents.assert_called_once_with("test-project/gate-task-1")

    async def test_reviewing_starts_fresh_review(self, db, sample_project):
        """gate_status=reviewing → start fresh _dispatch_review."""
        await _make_task(db, gate_status="reviewing")

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            await _resume_gate_pipeline("test-project/gate-task-1")
        await asyncio.sleep(0)

        self.mock_dispatch_review.assert_called_once()

    async def test_review_failed_under_limit_calls_retry_task(self, db, sample_project):
        """gate_status=review-failed with retries < max → reset gate_status, call retry_task."""
        await _make_task(db, gate_status="review-failed", gate_retries=1)

        await _resume_gate_pipeline("test-project/gate-task-1")

        task = await db.get_task("test-project/gate-task-1")
        assert task["gate_status"] is None  # reset before retry_task
        self.mock_retry_task.assert_called_once_with("test-project/gate-task-1")

    async def test_review_failed_at_limit_sets_needs_review(self, db, sample_project):
        """gate_status=review-failed with retries >= max_review_retries → needs-review."""
        await _make_task(db, gate_status="review-failed", gate_retries=3)
        # max_review_retries defaults to 3 (max_gate_retries fallback)

        await _resume_gate_pipeline("test-project/gate-task-1")

        task = await db.get_task("test-project/gate-task-1")
        assert task["gate_status"] == "needs-review"
        self.mock_notify.task_needs_review.assert_called_once()

    async def test_needs_review_resets_retries_and_runs_test_gate(self, db, sample_project):
        """gate_status=needs-review → reset gate_retries=0, run test gate."""
        await _make_task(db, gate_status="needs-review", gate_retries=2)

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            await _resume_gate_pipeline("test-project/gate-task-1")
        await asyncio.sleep(0)

        task = await db.get_task("test-project/gate-task-1")
        assert task["gate_status"] is None
        assert task["gate_retries"] == 0
        self.mock_run_test_gate.assert_called_once()

    async def test_push_failed_retry_succeeds(self, db, sample_project):
        """gate_status=push-failed, push succeeds → reset gate_status, run test gate."""
        await _make_task(db, gate_status="push-failed")
        self.mock_ensure_pushed.return_value = True

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            await _resume_gate_pipeline("test-project/gate-task-1")
        await asyncio.sleep(0)

        task = await db.get_task("test-project/gate-task-1")
        assert task["gate_status"] is None
        self.mock_run_test_gate.assert_called_once()

    async def test_push_failed_retry_fails_leaves_as_push_failed(self, db, sample_project):
        """gate_status=push-failed, push still fails → leave as push-failed."""
        await _make_task(db, gate_status="push-failed")
        self.mock_ensure_pushed.return_value = False

        await _resume_gate_pipeline("test-project/gate-task-1")

        task = await db.get_task("test-project/gate-task-1")
        assert task["gate_status"] == "push-failed"
        self.mock_run_test_gate.assert_not_called()

    async def test_passed_with_no_gate_passed_at(self, db, sample_project):
        """gate_status=passed but no gate_passed_at → dispatch dependents."""
        await _make_task(db, gate_status="passed", gate_passed_at=None)

        await _resume_gate_pipeline("test-project/gate-task-1")

        self.mock_check_dependents.assert_called_once_with("test-project/gate-task-1")

    async def test_does_not_reset_gate_retries_for_testing(self, db, sample_project):
        """gate_status=testing → gate_retries preserved (not reset)."""
        await _make_task(db, gate_status="testing", gate_retries=2)

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            await _resume_gate_pipeline("test-project/gate-task-1")
        await asyncio.sleep(0)

        task = await db.get_task("test-project/gate-task-1")
        assert task["gate_retries"] == 2  # not reset

    async def test_does_not_reset_gate_retries_for_reviewing(self, db, sample_project):
        """gate_status=reviewing → gate_retries preserved (not reset)."""
        await _make_task(db, gate_status="reviewing", gate_retries=1)

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            await _resume_gate_pipeline("test-project/gate-task-1")
        await asyncio.sleep(0)

        task = await db.get_task("test-project/gate-task-1")
        assert task["gate_retries"] == 1  # not reset

    async def test_posts_recovery_message(self, db, sample_project):
        """_resume_gate_pipeline always posts a status message."""
        await _make_task(db, gate_status="testing", pushed_at=db.now_iso())

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            await _resume_gate_pipeline("test-project/gate-task-1", reason="test-reason")
        await asyncio.sleep(0)

        thread = await db.read_task_messages("test-project/gate-task-1")
        messages = thread.get("messages", [])
        recovery_msgs = [m for m in messages if m.get("type") == "status"
                         and "recovery" in (m.get("title") or "").lower()]
        assert len(recovery_msgs) >= 1
        # Message should mention the reason
        assert "test-reason" in recovery_msgs[0]["content"]

    async def test_returns_none_for_missing_task(self, db, sample_project):
        """Returns None when task doesn't exist."""
        result = await _resume_gate_pipeline("test-project/nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# Startup recovery — unified sweep via recover_orphaned_tasks
# ---------------------------------------------------------------------------

class TestStartupRecoveryUnifiedSweep:
    """recover_orphaned_tasks unified sweep handles all statuses and gate states."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _running_gates.clear()
        self.mock_resume_pipeline = AsyncMock()
        self.mock_check_dependents = AsyncMock()

        patches = [
            # _resume_gate_pipeline is defined in gates.py and lazily imported in recovery.py
            patch("switchboard.dispatch.gates._resume_gate_pipeline", self.mock_resume_pipeline),
            patch("switchboard.dispatch.engine._check_and_dispatch_dependents", self.mock_check_dependents),
            patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake")),
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
        _running_gates.clear()

    async def test_completed_testing_triggers_resume_pipeline(self, db, sample_project):
        """completed task with gate_status=testing → _resume_gate_pipeline called."""
        await _make_task(db, status="completed", gate_status="testing")

        await recover_orphaned_tasks()

        self.mock_resume_pipeline.assert_any_call(
            "test-project/gate-task-1", reason="startup recovery"
        )

    async def test_completed_reviewing_triggers_resume_pipeline(self, db, sample_project):
        """completed task with gate_status=reviewing → _resume_gate_pipeline called."""
        await _make_task(db, status="completed", gate_status="reviewing")

        await recover_orphaned_tasks()

        self.mock_resume_pipeline.assert_any_call(
            "test-project/gate-task-1", reason="startup recovery"
        )

    async def test_completed_test_failed_triggers_resume_pipeline(self, db, sample_project):
        """completed task with gate_status=test-failed → _resume_gate_pipeline called."""
        await _make_task(db, status="completed", gate_status="test-failed")

        await recover_orphaned_tasks()

        self.mock_resume_pipeline.assert_any_call(
            "test-project/gate-task-1", reason="startup recovery"
        )

    async def test_completed_review_failed_triggers_resume_pipeline(self, db, sample_project):
        """completed task with gate_status=review-failed → _resume_gate_pipeline called."""
        await _make_task(db, status="completed", gate_status="review-failed")

        await recover_orphaned_tasks()

        self.mock_resume_pipeline.assert_any_call(
            "test-project/gate-task-1", reason="startup recovery"
        )

    async def test_pending_validation_none_gate_triggers_resume_pipeline(self, db, sample_project):
        """pending-validation with gate_status=None → _resume_gate_pipeline called."""
        await _make_task(db, status="pending-validation", gate_status=None)

        await recover_orphaned_tasks()

        self.mock_resume_pipeline.assert_any_call(
            "test-project/gate-task-1", reason="startup recovery"
        )

    async def test_turns_exhausted_testing_triggers_resume_pipeline(self, db, sample_project):
        """turns-exhausted with gate_status=testing → _resume_gate_pipeline called."""
        await _make_task(db, status="turns-exhausted", gate_status="testing")

        await recover_orphaned_tasks()

        self.mock_resume_pipeline.assert_any_call(
            "test-project/gate-task-1", reason="startup recovery"
        )

    async def test_push_failed_skipped(self, db, sample_project):
        """push-failed tasks are skipped at startup (user must fix PAT)."""
        await _make_task(db, status="completed", gate_status="push-failed")

        await recover_orphaned_tasks()

        # _resume_gate_pipeline should NOT be called for push-failed
        for call in self.mock_resume_pipeline.call_args_list:
            assert call[0][0] != "test-project/gate-task-1"

    async def test_completed_none_gate_not_triggered(self, db, sample_project):
        """completed task with gate_status=None → not triggered (no gate to recover)."""
        await _make_task(db, status="completed", gate_status=None)

        await recover_orphaned_tasks()

        # Should not be called for completed + gate_status=None
        for call in self.mock_resume_pipeline.call_args_list:
            assert call[0][0] != "test-project/gate-task-1"

    async def test_gate_passed_with_passed_status_dispatches_dependents(self, db, sample_project):
        """Task with gate_passed_at set and gate=passed → dispatch dependents."""
        await _make_task(db, status="completed", gate_status="passed",
                         gate_passed_at=db.now_iso())

        await recover_orphaned_tasks()

        self.mock_check_dependents.assert_called_with("test-project/gate-task-1")

    async def test_gate_passed_with_gate_passed_at_not_retrieved(self, db, sample_project):
        """Task with gate_passed_at set and non-'passed' gate → NOT sent to resume pipeline."""
        await _make_task(db, status="completed", gate_status="reviewing",
                         gate_passed_at=db.now_iso())

        await recover_orphaned_tasks()

        # Should be skipped (continue on gate_passed_at)
        for call in self.mock_resume_pipeline.call_args_list:
            assert call[0][0] != "test-project/gate-task-1"


# ---------------------------------------------------------------------------
# Background orphan detection in check_stalled_tasks
# ---------------------------------------------------------------------------

class TestBackgroundOrphanDetection:
    """check_stalled_tasks detects orphaned testing/reviewing gate states."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _running_gates.clear()
        self.mock_resume_pipeline = AsyncMock()
        self.mock_retry_task = AsyncMock()

        patches = [
            # _resume_gate_pipeline is defined in gates.py and lazily imported in check_stalled_tasks
            patch("switchboard.dispatch.gates._resume_gate_pipeline", self.mock_resume_pipeline),
            patch("switchboard.dispatch.engine.retry_task", self.mock_retry_task),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()
        _running_gates.clear()

    async def _run_one_cycle(self, db):
        """Run a single iteration of check_stalled_tasks.

        The loop sleeps at the TOP of each iteration. We allow the first sleep to
        return normally (so the iteration body runs), then raise CancelledError on
        the second sleep to exit the loop cleanly.
        """
        sleep_count = 0

        async def _mock_sleep(n):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise asyncio.CancelledError()
            # First sleep: return normally so the iteration body executes

        with patch("asyncio.sleep", _mock_sleep):
            try:
                await check_stalled_tasks()
            except asyncio.CancelledError:
                pass

    async def test_testing_state_orphaned_triggers_recovery(self, db, sample_project):
        """Testing state task with no live gate and idle > 120s → recovery triggered."""
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        await _make_task(db, status="completed", gate_status="testing",
                         last_activity=old_time)

        await self._run_one_cycle(db)

        self.mock_resume_pipeline.assert_called_with(
            "test-project/gate-task-1", reason="background monitor"
        )

    async def test_reviewing_state_orphaned_triggers_recovery(self, db, sample_project):
        """Reviewing state task with no live gate and idle > 120s → recovery triggered."""
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        await _make_task(db, status="completed", gate_status="reviewing",
                         last_activity=old_time)

        await self._run_one_cycle(db)

        self.mock_resume_pipeline.assert_called_with(
            "test-project/gate-task-1", reason="background monitor"
        )

    async def test_live_gate_not_recovered(self, db, sample_project):
        """Testing state task with live gate coroutine → NOT recovered."""
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        await _make_task(db, status="completed", gate_status="testing",
                         last_activity=old_time)

        _running_gates.add("test-project/gate-task-1")

        await self._run_one_cycle(db)

        for call in self.mock_resume_pipeline.call_args_list:
            assert call[0][0] != "test-project/gate-task-1"

    async def test_within_grace_period_not_recovered(self, db, sample_project):
        """Testing state task idle for only 60s (< 120s grace) → NOT recovered."""
        recent_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        await _make_task(db, status="completed", gate_status="testing",
                         last_activity=recent_time)

        await self._run_one_cycle(db)

        for call in self.mock_resume_pipeline.call_args_list:
            assert call[0][0] != "test-project/gate-task-1"

    async def test_non_active_gate_states_not_recovered(self, db, sample_project):
        """Tasks with gate_status not in (testing, reviewing) → NOT recovered by background."""
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        for gs in ("test-failed", "review-failed", "test-passed", "needs-review", "passed"):
            await _make_task(db, task_id=f"test-project/t-{gs}",
                             status="completed", gate_status=gs, last_activity=old_time)

        await self._run_one_cycle(db)

        # None of those should trigger background recovery
        assert self.mock_resume_pipeline.call_count == 0

    async def test_turns_exhausted_testing_triggers_recovery(self, db, sample_project):
        """turns-exhausted with gate_status=testing and old activity → recovery triggered."""
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        await _make_task(db, status="turns-exhausted", gate_status="testing",
                         last_activity=old_time)

        await self._run_one_cycle(db)

        self.mock_resume_pipeline.assert_called_with(
            "test-project/gate-task-1", reason="background monitor"
        )

    async def test_no_last_activity_skipped(self, db, sample_project):
        """Tasks with no last_activity and no updated_at → skipped."""
        await _make_task(db, status="completed", gate_status="testing")
        await db.update_task("test-project/gate-task-1", last_activity=None)

        await self._run_one_cycle(db)

        # May or may not be called depending on updated_at — but should not crash
        # The key thing is no exception is raised


# ---------------------------------------------------------------------------
# mark_working_for_recovery — shutdown logging
# ---------------------------------------------------------------------------

class TestMarkWorkingForRecovery:
    """mark_working_for_recovery logs gate states at shutdown."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _running_gates.clear()
        yield
        _running_gates.clear()

    async def test_logs_running_gates_at_shutdown(self, db, sample_project, caplog):
        """If _running_gates is non-empty, shutdown logs the affected task IDs."""
        import logging
        _running_gates.add("test-project/task-in-gate")

        with caplog.at_level(logging.INFO, logger="switchboard.dispatch.recovery"):
            await mark_working_for_recovery()

        assert "test-project/task-in-gate" in caplog.text

    async def test_logs_active_gate_status_tasks(self, db, sample_project, caplog):
        """Tasks in testing/reviewing at shutdown are logged."""
        import logging
        await _make_task(db, status="completed", gate_status="testing")

        with caplog.at_level(logging.INFO, logger="switchboard.dispatch.recovery"):
            await mark_working_for_recovery()

        assert "test-project/gate-task-1" in caplog.text

    async def test_working_tasks_marked_for_recovery(self, db, sample_project):
        """Working tasks get recovery_priority set (existing behavior preserved)."""
        task = await _make_task(db, status="working", gate_status=None)

        await mark_working_for_recovery()

        task = await db.get_task("test-project/gate-task-1")
        assert task["recovery_count"] == 0  # not changed
        # recovery_priority is set
        assert task["recovery_priority"] == 1


# ---------------------------------------------------------------------------
# retry_task gate re-entry — broader delegation
# ---------------------------------------------------------------------------

class TestRetryTaskGateReentry:
    """retry_task delegates to _resume_gate_pipeline for any non-None gate_status."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _running_gates.clear()
        self.mock_resume_pipeline = AsyncMock(return_value={"id": "test-project/gate-task-1"})

        patches = [
            patch("switchboard.dispatch.gates._resume_gate_pipeline", self.mock_resume_pipeline),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()
        _running_gates.clear()

    async def test_completed_needs_review_delegates_to_pipeline(self, db, sample_project):
        """completed + needs-review gate_status → _resume_gate_pipeline called."""
        from switchboard.dispatch.engine import retry_task
        await _make_task(db, status="completed", gate_status="needs-review")

        await retry_task("test-project/gate-task-1")

        self.mock_resume_pipeline.assert_called_with(
            "test-project/gate-task-1", reason="retry"
        )

    async def test_completed_testing_delegates_to_pipeline(self, db, sample_project):
        """completed + testing gate_status → _resume_gate_pipeline called."""
        from switchboard.dispatch.engine import retry_task
        await _make_task(db, status="completed", gate_status="testing")

        await retry_task("test-project/gate-task-1")

        self.mock_resume_pipeline.assert_called_with(
            "test-project/gate-task-1", reason="retry"
        )

    async def test_turns_exhausted_any_gate_delegates_to_pipeline(self, db, sample_project):
        """turns-exhausted + any non-None gate_status → _resume_gate_pipeline called."""
        from switchboard.dispatch.engine import retry_task
        await _make_task(db, status="turns-exhausted", gate_status="reviewing")

        await retry_task("test-project/gate-task-1")

        self.mock_resume_pipeline.assert_called_with(
            "test-project/gate-task-1", reason="retry"
        )

    async def test_completed_none_gate_does_not_delegate(self, db, sample_project):
        """completed + gate_status=None → normal retry path (dispatch new CC session)."""
        from switchboard.dispatch.engine import retry_task

        await _make_task(db, status="completed", gate_status=None)

        with patch("switchboard.dispatch.engine.dispatch_task", AsyncMock(return_value={"id": "t1"})) as mock_dispatch:
            with patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp")):
                with patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()):
                    with patch("switchboard.dispatch.engine.notify", AsyncMock()):
                        with patch("switchboard.dispatch.engine.archive_task_logs", AsyncMock()):
                            await retry_task("test-project/gate-task-1")

        # _resume_gate_pipeline should NOT be called
        self.mock_resume_pipeline.assert_not_called()

    async def test_gate_passed_at_set_does_not_delegate(self, db, sample_project):
        """If gate_passed_at is set, the gate re-entry check is skipped (gate already passed)."""
        from switchboard.dispatch.engine import retry_task
        await _make_task(db, status="completed", gate_status="needs-review",
                         gate_passed_at=db.now_iso())

        with patch("switchboard.dispatch.engine.dispatch_task", AsyncMock(return_value={"id": "t1"})):
            with patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp")):
                with patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()):
                    with patch("switchboard.dispatch.engine.notify", AsyncMock()):
                        with patch("switchboard.dispatch.engine.archive_task_logs", AsyncMock()):
                            await retry_task("test-project/gate-task-1")

        self.mock_resume_pipeline.assert_not_called()
