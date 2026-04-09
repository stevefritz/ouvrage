"""Tests for gate re-entry loop hotfix.

Verifies that retry_task correctly routes based on whether the gate was
INTERRUPTED (process died mid-flight) vs REJECTED (code needs changes):

- Interrupted: testing, reviewing, test-passed → re-enter gate pipeline
- Rejected:    test-failed, review-failed, needs-review → launch CC with feedback

Before this fix, retry_task delegated ALL non-None gate states to
_resume_gate_pipeline, which re-ran the test gate for needs-review.
This caused an infinite loop: review rejects → gate retry → tests pass →
review rejects → gate retry → forever.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_completed_task(db, gate_status, gate_retries=0):
    """Create a completed task in the given gate state."""
    task = await db.create_task(
        id="test-project/reentry-task",
        project_id="test-project",
        goal="Gate re-entry fix test",
    )
    await db.update_task(
        "test-project/reentry-task",
        status="completed",
        gate_status=gate_status,
        gate_retries=gate_retries,
        worktree_path="/tmp/fake-worktree",
        pushed_at=db.now_iso(),
    )
    return await db.get_task("test-project/reentry-task")


# ---------------------------------------------------------------------------
# Spec test 1: retry_task + review-failed → CC session (not test gate)
# ---------------------------------------------------------------------------

class TestRetryTaskReviewFailed:
    """retry_task with gate_status=review-failed must launch CC, not re-run gates."""

    async def test_review_failed_launches_cc_not_test_gate(self, db, sample_project):
        """Spec test 1: retry_task with gate_status=review-failed launches CC session."""
        await _make_completed_task(db, "review-failed", gate_retries=1)

        mock_test_gate = AsyncMock()
        mock_run_sdk = AsyncMock()
        with patch("switchboard.dispatch.gates._run_test_gate", mock_test_gate), \
             patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake-wt")), \
             patch("switchboard.dispatch.internals.setup_hook_config", AsyncMock()), \
             patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()), \
             patch("switchboard.dispatch.engine.archive_task_logs", AsyncMock()), \
             patch("switchboard.dispatch.engine._setup_log_dir", AsyncMock(return_value="/tmp/fake-wt/.switchboard")), \
             patch("switchboard.dispatch.engine._write_dispatch_log"), \
             patch("switchboard.dispatch.engine._run_sdk_session", mock_run_sdk):
            from switchboard.dispatch.engine import retry_task
            await retry_task("test-project/reentry-task")

        await asyncio.sleep(0)
        mock_test_gate.assert_not_called()
        mock_run_sdk.assert_called_once()


# ---------------------------------------------------------------------------
# Spec test 2: retry_task + test-failed → CC session (not test gate)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Spec test 3: retry_task + testing → _run_test_gate (not CC)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Spec test 4: retry_task + reviewing → _dispatch_review (not CC)
# ---------------------------------------------------------------------------

class TestRetryTaskReviewing:
    """retry_task with gate_status=reviewing must re-enter gate pipeline (interrupted)."""

    async def test_reviewing_calls_dispatch_review_not_cc(self, db, sample_project):
        """Spec test 4: retry_task with gate_status=reviewing re-dispatches review."""
        await _make_completed_task(db, "reviewing")

        mock_dispatch_review = AsyncMock()
        mock_dispatch = AsyncMock()
        with patch("switchboard.dispatch.gates._dispatch_review", mock_dispatch_review), \
             patch("switchboard.dispatch.engine.dispatch_task", mock_dispatch):
            from switchboard.dispatch.engine import retry_task
            await retry_task("test-project/reentry-task")

        await asyncio.sleep(0)
        mock_dispatch_review.assert_called_once()
        task_id_arg = mock_dispatch_review.call_args[0][0]
        assert task_id_arg == "test-project/reentry-task"
        mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# Spec tests 5 & 6: _resume_gate_pipeline return values
# ---------------------------------------------------------------------------

class TestResumePipelineReturnValues:
    """_resume_gate_pipeline returns True for interrupted states, False for rejections."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        import os as _os
        from switchboard.dispatch._state import _running_gates
        _running_gates.clear()

        _real_exists = _os.path.exists

        def _fake_exists(p):
            if p == "/tmp/fake-worktree":
                return True
            return _real_exists(p)

        patches = [
            patch("switchboard.dispatch.gates._run_test_gate", AsyncMock()),
            patch("switchboard.dispatch.gates._dispatch_review", AsyncMock()),
            patch("switchboard.dispatch.gates.notify", AsyncMock()),
            patch("switchboard.dispatch.engine.retry_task", AsyncMock()),
            patch("switchboard.dispatch.engine._check_and_dispatch_dependents", AsyncMock()),
            patch("switchboard.dispatch.gates.os.path.exists", side_effect=_fake_exists),
        ]
        self._patches = [p.start() for p in patches]
        yield
        for p in patches:
            p.stop()
        _running_gates.clear()

    async def _make_task(self, db, gate_status, gate_retries=0):
        task = await db.create_task(
            id="test-project/return-val-task",
            project_id="test-project",
            goal="Return value test",
        )
        await db.update_task(
            "test-project/return-val-task",
            status="completed",
            gate_status=gate_status,
            gate_retries=gate_retries,
            worktree_path="/tmp/fake-worktree",
            pushed_at=db.now_iso(),
        )


