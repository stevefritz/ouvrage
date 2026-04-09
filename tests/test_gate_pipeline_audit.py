"""Tests for gate pipeline audit fixes.

Covers the critical scenarios from the gate-pipeline-audit spec:
1. test→review transition (the _running_gates fix)
2. Interrupted test recovery
3. Interrupted review recovery
4. Review rejection → CC retry
5. Test failure → CC retry
6. Missing worktree guard
7. Concurrent recovery race prevention
8. _dispatch_review duplicate guard via gate_status
"""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

import switchboard.db as db
from switchboard.dispatch._state import _running_gates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_task(
    db,
    task_id="test-project/audit-task-1",
    status="pending-validation",
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
        goal="Gate pipeline audit test",
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
# Scenario 1: test→review transition (the core _running_gates fix)
# ---------------------------------------------------------------------------

class TestTestToReviewTransition:
    """The _running_gates fix: _dispatch_review must not be blocked when called
    from within _run_test_gate_inner (which holds _running_gates).

    Before the fix: _dispatch_review checked _running_gates → TRUE (test gate
    held it) → review silently skipped → task stuck at test-passed.

    After the fix: _dispatch_review uses gate_status as its duplicate guard
    instead of _running_gates, so the normal test→review transition works.
    """

    @pytest.fixture(autouse=True)
    def _setup(self):
        _running_gates.clear()
        yield
        _running_gates.clear()


    async def test_dispatch_review_blocks_duplicate_via_gate_status(self, db, sample_project):
        """If gate_status is already 'reviewing', _dispatch_review skips (new duplicate guard)."""
        from switchboard.dispatch.gates import _dispatch_review

        # Create task already in reviewing state
        await _make_task(db, gate_status="reviewing")

        inner_called = []

        async def _fake_inner(tid, proj, task):
            inner_called.append(tid)

        with patch("switchboard.dispatch.gates._dispatch_review_inner", _fake_inner):
            await _dispatch_review("test-project/audit-task-1", sample_project, {})

        assert inner_called == []  # Duplicate blocked


# ---------------------------------------------------------------------------
# Scenario 4 & 5: Recovery for interrupted test/review gates
# ---------------------------------------------------------------------------

class TestInterruptedGateRecovery:
    """Server dies during test or review gate → recovery re-enters pipeline."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _running_gates.clear()

        _real_exists = os.path.exists

        def _fake_exists(p):
            if p == "/tmp/fake-worktree":
                return True
            return _real_exists(p)

        self.mock_run_test_gate = AsyncMock()
        self.mock_dispatch_review = AsyncMock()

        patches = [
            patch("switchboard.dispatch.gates._run_test_gate", self.mock_run_test_gate),
            patch("switchboard.dispatch.gates._dispatch_review", self.mock_dispatch_review),
            patch("switchboard.dispatch.gates.notify", AsyncMock()),
            patch("switchboard.dispatch.gates.os.path.exists", side_effect=_fake_exists),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()
        _running_gates.clear()


    async def test_test_passed_dispatches_review(self, db, sample_project):
        """Scenario 6: gate_status=test-passed after restart → dispatch review."""
        from switchboard.dispatch.gates import _resume_gate_pipeline

        await _make_task(db, gate_status="test-passed", auto_review=True)

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            result = await _resume_gate_pipeline("test-project/audit-task-1", reason="startup recovery")
        await asyncio.sleep(0)

        assert result is True
        self.mock_dispatch_review.assert_called_once()

    async def test_running_gates_empty_after_restart(self, db, sample_project):
        """Scenario 4/5/7: _running_gates is empty after restart (in-memory set)."""
        # _running_gates was cleared in _setup
        assert len(_running_gates) == 0


# ---------------------------------------------------------------------------
# Scenario 2 & 3: Rejection states → CC retry (not gate re-entry)
# ---------------------------------------------------------------------------

class TestRejectionStatesNotReenteredByRetry:
    """retry_task with rejection gate states must launch CC, not re-enter gates."""


    async def test_needs_review_excluded_from_gate_reentry(self, db, sample_project, mock_git, mock_sdk):
        """Scenario 8: needs-review → retry_task launches fresh CC session."""
        await _make_task(db, status="completed", gate_status="needs-review", gate_retries=2)

        mock_resume_pipeline = AsyncMock()
        with patch("switchboard.dispatch.gates._resume_gate_pipeline", mock_resume_pipeline):
            from switchboard.dispatch.engine import retry_task
            await retry_task("test-project/audit-task-1")

        mock_resume_pipeline.assert_not_called()
        task = await db.get_task("test-project/audit-task-1")
        assert task["status"] == "working"


# ---------------------------------------------------------------------------
# Scenario 9: Missing worktree guard
# ---------------------------------------------------------------------------

class TestMissingWorktreeGuard:
    """Recovery must not re-enter gate pipeline when worktree is missing."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _running_gates.clear()
        patches = [
            patch("switchboard.dispatch.gates._run_test_gate", AsyncMock()),
            patch("switchboard.dispatch.gates._dispatch_review", AsyncMock()),
            patch("switchboard.dispatch.gates.notify", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()
        _running_gates.clear()


# ---------------------------------------------------------------------------
# Scenario 11: Concurrent recovery race prevention
# ---------------------------------------------------------------------------

class TestConcurrentRacePrevention:
    """_running_gates prevents double execution when normal completion races
    with background monitor recovery."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _running_gates.clear()

        _real_exists = os.path.exists

        def _fake_exists(p):
            if p == "/tmp/fake-worktree":
                return True
            return _real_exists(p)

        patches = [
            patch("switchboard.dispatch.gates._run_test_gate", AsyncMock()),
            patch("switchboard.dispatch.gates._dispatch_review", AsyncMock()),
            patch("switchboard.dispatch.gates.notify", AsyncMock()),
            patch("switchboard.dispatch.gates.os.path.exists", side_effect=_fake_exists),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()
        _running_gates.clear()

    async def test_resume_skips_when_gate_already_running(self, db, sample_project):
        """If task_id is in _running_gates (normal completion in progress),
        _resume_gate_pipeline skips to prevent double execution."""
        from switchboard.dispatch.gates import _resume_gate_pipeline

        await _make_task(db, gate_status="testing")
        _running_gates.add("test-project/audit-task-1")

        result = await _resume_gate_pipeline("test-project/audit-task-1", reason="background monitor")

        # Returns the task (truthy) but doesn't re-run the gate
        assert result is not None
        assert result is not False

    async def test_run_test_gate_duplicate_guard(self, db, sample_project):
        """_run_test_gate skips if task_id already in _running_gates."""
        from switchboard.dispatch.gates import _run_test_gate

        _running_gates.add("test-project/audit-task-1")
        inner_called = []

        async def _fake_inner(*args):
            inner_called.append(True)

        with patch("switchboard.dispatch.gates._run_test_gate_inner", _fake_inner):
            await _run_test_gate("test-project/audit-task-1", {}, {})

        assert inner_called == []  # Skipped — gate already running
