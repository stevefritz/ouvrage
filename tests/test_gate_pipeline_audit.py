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

    async def test_dispatch_review_works_when_running_gates_held(self, db, sample_project):
        """_dispatch_review proceeds even when task_id is in _running_gates.

        This is the exact bug scenario: _run_test_gate adds to _running_gates,
        then _run_test_gate_inner calls _dispatch_review. The review must run.
        """
        from switchboard.dispatch.gates import _dispatch_review

        # Simulate: task is in _running_gates (held by _run_test_gate)
        _running_gates.add("test-project/audit-task-1")

        # Create task in test-passed state (not yet reviewing)
        await _make_task(db, gate_status="test-passed")

        inner_called = []

        async def _fake_inner(tid, proj, task):
            inner_called.append(tid)

        with patch("switchboard.dispatch.gates._dispatch_review_inner", _fake_inner):
            await _dispatch_review("test-project/audit-task-1", sample_project, {})

        # The review inner function MUST be called (was skipped before the fix)
        assert inner_called == ["test-project/audit-task-1"]

    async def test_dispatch_review_adds_to_running_gates_for_liveness(self, db, sample_project):
        """_dispatch_review still adds to _running_gates for background monitor liveness."""
        from switchboard.dispatch.gates import _dispatch_review

        # Create task NOT in reviewing state
        await _make_task(db, gate_status="test-passed")

        captured_in_gates = []

        async def _fake_inner(tid, proj, task):
            captured_in_gates.append(tid in _running_gates)

        with patch("switchboard.dispatch.gates._dispatch_review_inner", _fake_inner):
            await _dispatch_review("test-project/audit-task-1", sample_project, {})

        assert captured_in_gates == [True]

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

    async def test_testing_interrupted_reruns_test_gate(self, db, sample_project):
        """Scenario 4: gate_status=testing after restart → re-run test gate."""
        from switchboard.dispatch.gates import _resume_gate_pipeline

        await _make_task(db, gate_status="testing")

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            result = await _resume_gate_pipeline("test-project/audit-task-1", reason="startup recovery")
        await asyncio.sleep(0)

        assert result is True
        self.mock_run_test_gate.assert_called_once()
        # gate_retries should NOT be reset (interrupted, not a fresh attempt)
        task = await db.get_task("test-project/audit-task-1")
        assert task["gate_retries"] == 0  # was 0, stayed 0

    async def test_testing_interrupted_preserves_gate_retries(self, db, sample_project):
        """Recovery does not reset gate_retries for interrupted test gates."""
        from switchboard.dispatch.gates import _resume_gate_pipeline

        await _make_task(db, gate_status="testing", gate_retries=2)

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            await _resume_gate_pipeline("test-project/audit-task-1", reason="startup recovery")
        await asyncio.sleep(0)

        task = await db.get_task("test-project/audit-task-1")
        assert task["gate_retries"] == 2  # preserved

    async def test_reviewing_interrupted_dispatches_fresh_review(self, db, sample_project):
        """Scenario 5: gate_status=reviewing after restart → fresh review dispatch."""
        from switchboard.dispatch.gates import _resume_gate_pipeline

        await _make_task(db, gate_status="reviewing")

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            result = await _resume_gate_pipeline("test-project/audit-task-1", reason="startup recovery")
        await asyncio.sleep(0)

        assert result is True
        self.mock_dispatch_review.assert_called_once()

    async def test_reviewing_recovery_resets_gate_status_before_dispatch(self, db, sample_project):
        """Scenario 5 detail: recovery resets gate_status to test-passed so
        _dispatch_review's duplicate guard (gate_status==reviewing) doesn't block it."""
        from switchboard.dispatch.gates import _resume_gate_pipeline

        await _make_task(db, gate_status="reviewing")

        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            await _resume_gate_pipeline("test-project/audit-task-1", reason="startup recovery")
        await asyncio.sleep(0)

        # After _resume_gate_pipeline, verify that it called update_task to reset gate_status
        # before dispatching review. The mock_dispatch_review receives the task arg — check it.
        self.mock_dispatch_review.assert_called_once()
        call_args = self.mock_dispatch_review.call_args
        task_arg = call_args[0][2]  # third positional arg is task dict
        assert task_arg["gate_status"] == "test-passed"

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

    async def test_review_failed_excluded_from_gate_reentry(self, db, sample_project, mock_git, mock_sdk):
        """Scenario 2: review-failed → retry_task launches fresh CC session."""
        await _make_task(db, status="completed", gate_status="review-failed", gate_retries=1)

        mock_resume_pipeline = AsyncMock()
        with patch("switchboard.dispatch.gates._resume_gate_pipeline", mock_resume_pipeline):
            from switchboard.dispatch.engine import retry_task
            await retry_task("test-project/audit-task-1")

        # review-failed is NOT an interrupted state — CC gets a fresh session, not gate re-entry
        mock_resume_pipeline.assert_not_called()
        task = await db.get_task("test-project/audit-task-1")
        assert task["status"] == "working"

    async def test_test_failed_excluded_from_gate_reentry(self, db, sample_project, mock_git, mock_sdk):
        """Scenario 3: test-failed → retry_task launches fresh CC session."""
        await _make_task(db, status="completed", gate_status="test-failed", gate_retries=1)

        mock_resume_pipeline = AsyncMock()
        with patch("switchboard.dispatch.gates._resume_gate_pipeline", mock_resume_pipeline):
            from switchboard.dispatch.engine import retry_task
            await retry_task("test-project/audit-task-1")

        mock_resume_pipeline.assert_not_called()
        task = await db.get_task("test-project/audit-task-1")
        assert task["status"] == "working"

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

    async def test_retry_task_clears_gate_state_for_fresh_run(self, db, sample_project, mock_git, mock_sdk):
        """retry_task resets gate_status and gate_retries for fresh CC run."""
        await _make_task(db, status="completed", gate_status="review-failed", gate_retries=2)

        from switchboard.dispatch.engine import retry_task
        await retry_task("test-project/audit-task-1")

        # After retry_task, gate state is cleared before dispatch_task is called
        task = await db.get_task("test-project/audit-task-1")
        assert task["gate_status"] is None
        assert task["gate_retries"] == 0


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

    async def test_missing_worktree_sets_needs_review(self, db, sample_project):
        """Scenario 9: worktree released → recovery sets gate_status=needs-review."""
        from switchboard.dispatch.gates import _resume_gate_pipeline

        # worktree_path is None (released)
        await _make_task(db, gate_status="testing", worktree_path=None)

        result = await _resume_gate_pipeline("test-project/audit-task-1", reason="test")

        assert result is False
        task = await db.get_task("test-project/audit-task-1")
        assert task["gate_status"] == "needs-review"

    async def test_nonexistent_worktree_sets_needs_review(self, db, sample_project):
        """Worktree path set but directory doesn't exist → needs-review."""
        from switchboard.dispatch.gates import _resume_gate_pipeline

        await _make_task(db, gate_status="testing",
                         worktree_path="/tmp/nonexistent-worktree-xyz-999")

        result = await _resume_gate_pipeline("test-project/audit-task-1", reason="test")

        assert result is False
        task = await db.get_task("test-project/audit-task-1")
        assert task["gate_status"] == "needs-review"

    async def test_worktree_guard_posts_message(self, db, sample_project):
        """Missing worktree posts a status message explaining the situation."""
        from switchboard.dispatch.gates import _resume_gate_pipeline

        await _make_task(db, gate_status="reviewing", worktree_path=None)

        await _resume_gate_pipeline("test-project/audit-task-1", reason="background monitor")

        thread = await db.read_task_messages("test-project/audit-task-1")
        messages = thread.get("messages", [])
        worktree_msgs = [m for m in messages if "worktree" in (m.get("title") or "").lower()]
        assert len(worktree_msgs) >= 1


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
