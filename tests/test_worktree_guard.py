"""Tests for worktree existence guards on gate recovery and retry paths.

Covers:
- _verify_worktree_exists: missing worktree → needs-review + message; existing → True
- _resume_gate_pipeline with missing worktree: returns False, sets needs-review
- _run_test_gate_inner with missing worktree: returns early, sets needs-review
- _dispatch_review_inner with missing worktree: returns early, sets needs-review
- retry_task gate re-entry with missing worktree: falls through to normal retry
- _recover_gate_subtask with missing parent worktree: sets needs-review, posts message
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import switchboard.db as db
from switchboard.dispatch._state import _running_gates
from switchboard.dispatch.gates import _verify_worktree_exists, _resume_gate_pipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_task(
    db,
    task_id="test-project/guard-task-1",
    status="completed",
    gate_status=None,
    worktree_path="/tmp/fake-worktree",
    pushed_at=None,
    gate_passed_at=None,
    auto_test=True,
    auto_review=True,
    parent_task_id=None,
    **kwargs,
):
    """Create a task in the given gate state."""
    await db.create_task(
        id=task_id,
        project_id="test-project",
        goal="Test worktree guard",
        auto_test=auto_test,
        auto_review=auto_review,
    )
    await db.update_task(
        task_id,
        status=status,
        gate_status=gate_status,
        worktree_path=worktree_path,
        pushed_at=pushed_at,
        gate_passed_at=gate_passed_at,
        parent_task_id=parent_task_id,
        **kwargs,
    )
    return await db.get_task(task_id)


# ---------------------------------------------------------------------------
# _verify_worktree_exists
# ---------------------------------------------------------------------------

class TestVerifyWorktreeExists:
    """_verify_worktree_exists marks needs-review and returns False when worktree is missing."""

    async def test_missing_worktree_returns_false(self, db, sample_project):
        """Non-existent path → returns False."""
        task = await _make_task(db, worktree_path="/tmp/definitely-does-not-exist-xyz123")
        result = await _verify_worktree_exists("test-project/guard-task-1", task, "test context")
        assert result is False

    async def test_missing_worktree_sets_needs_review(self, db, sample_project):
        """Non-existent path → task status set to needs-review."""
        task = await _make_task(db, worktree_path="/tmp/definitely-does-not-exist-xyz123")
        await _verify_worktree_exists("test-project/guard-task-1", task, "test context")
        refreshed = await db.get_task("test-project/guard-task-1")
        assert refreshed["status"] == "needs-review"

    async def test_missing_worktree_posts_message(self, db, sample_project):
        """Non-existent path → message posted with worktree path info."""
        task = await _make_task(db, worktree_path="/tmp/definitely-does-not-exist-xyz123")
        await _verify_worktree_exists("test-project/guard-task-1", task, "test context")
        thread = await db.read_task_messages("test-project/guard-task-1")
        messages = thread.get("messages", [])
        assert any("does not exist on disk" in m.get("content", "") for m in messages)

    async def test_none_worktree_returns_false(self, db, sample_project):
        """worktree_path=None → returns False."""
        task = await _make_task(db, worktree_path=None)
        result = await _verify_worktree_exists("test-project/guard-task-1", task, "test context")
        assert result is False

    async def test_existing_worktree_returns_true(self, db, sample_project, tmp_path):
        """Existing path → returns True, no status change."""
        task = await _make_task(db, worktree_path=str(tmp_path))
        result = await _verify_worktree_exists("test-project/guard-task-1", task, "test context")
        assert result is True

    async def test_existing_worktree_no_status_change(self, db, sample_project, tmp_path):
        """Existing path → task status is unchanged."""
        task = await _make_task(db, status="completed", worktree_path=str(tmp_path))
        await _verify_worktree_exists("test-project/guard-task-1", task, "test context")
        refreshed = await db.get_task("test-project/guard-task-1")
        assert refreshed["status"] == "completed"

    async def test_message_includes_context(self, db, sample_project):
        """Missing worktree message title includes the context string."""
        task = await _make_task(db, worktree_path="/tmp/definitely-does-not-exist-xyz123")
        await _verify_worktree_exists("test-project/guard-task-1", task, "my-context")
        thread = await db.read_task_messages("test-project/guard-task-1")
        messages = thread.get("messages", [])
        assert any("my-context" in m.get("title", "") for m in messages)


# ---------------------------------------------------------------------------
# _resume_gate_pipeline — missing worktree guard
# ---------------------------------------------------------------------------

class TestResumeGatePipelineMissingWorktree:
    """_resume_gate_pipeline skips all gate actions when worktree is missing."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        _running_gates.clear()
        self.mock_run_test_gate = AsyncMock()
        self.mock_dispatch_review = AsyncMock()
        self.mock_ensure_pushed = AsyncMock(return_value=True)

        patches = [
            patch("switchboard.dispatch.gates._run_test_gate", self.mock_run_test_gate),
            patch("switchboard.dispatch.gates._dispatch_review", self.mock_dispatch_review),
            patch("switchboard.git.operations._ensure_branch_pushed", self.mock_ensure_pushed),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()
        _running_gates.clear()

    async def test_missing_worktree_returns_false(self, db, sample_project):
        """Missing worktree → returns False."""
        await _make_task(db, gate_status="testing", worktree_path="/tmp/no-exist-xyz")
        result = await _resume_gate_pipeline("test-project/guard-task-1", reason="test")
        assert result is False

    async def test_missing_worktree_sets_needs_review(self, db, sample_project):
        """Missing worktree → task status set to needs-review."""
        await _make_task(db, gate_status="testing", worktree_path="/tmp/no-exist-xyz")
        await _resume_gate_pipeline("test-project/guard-task-1", reason="test")
        task = await db.get_task("test-project/guard-task-1")
        assert task["status"] == "needs-review"

    async def test_missing_worktree_posts_message(self, db, sample_project):
        """Missing worktree → message posted explaining the situation."""
        await _make_task(db, gate_status="testing", worktree_path="/tmp/no-exist-xyz")
        await _resume_gate_pipeline("test-project/guard-task-1", reason="test")
        thread = await db.read_task_messages("test-project/guard-task-1")
        messages = thread.get("messages", [])
        assert any("does not exist on disk" in m.get("content", "") for m in messages)

    async def test_missing_worktree_no_gate_triggered(self, db, sample_project):
        """Missing worktree → no test gate or review triggered."""
        await _make_task(db, gate_status="testing", worktree_path="/tmp/no-exist-xyz")
        await _resume_gate_pipeline("test-project/guard-task-1", reason="test")
        self.mock_run_test_gate.assert_not_called()
        self.mock_dispatch_review.assert_not_called()

    async def test_missing_worktree_none_path(self, db, sample_project):
        """worktree_path=None → returns False, no gate triggered."""
        await _make_task(db, gate_status="testing", worktree_path=None)
        result = await _resume_gate_pipeline("test-project/guard-task-1", reason="test")
        assert result is False
        self.mock_run_test_gate.assert_not_called()

    async def test_existing_worktree_proceeds(self, db, sample_project, tmp_path):
        """Existing worktree + gate_status=testing → test gate triggered normally."""
        await _make_task(
            db, gate_status="testing", worktree_path=str(tmp_path), pushed_at=db.now_iso()
        )
        with patch("asyncio.create_task", side_effect=lambda coro: asyncio.ensure_future(coro)):
            await _resume_gate_pipeline("test-project/guard-task-1", reason="test")
        await asyncio.sleep(0)
        self.mock_run_test_gate.assert_called_once()


# ---------------------------------------------------------------------------
# _run_test_gate_inner — missing worktree guard
# ---------------------------------------------------------------------------

class TestRunTestGateInnerMissingWorktree:
    """_run_test_gate_inner returns early and sets needs-review when worktree is missing."""

    async def test_missing_worktree_returns_early(self, db, sample_project):
        """Missing worktree → returns without running tests."""
        from switchboard.dispatch.gates import _run_test_gate_inner

        task = await _make_task(db, worktree_path="/tmp/no-exist-xyz")
        project = await db.get_project("test-project")

        # _run_test_streaming should NOT be called
        with patch("switchboard.dispatch.gates._run_test_streaming", AsyncMock(return_value=("", 0))) as mock_stream:
            await _run_test_gate_inner("test-project/guard-task-1", project, task)
            mock_stream.assert_not_called()

    async def test_missing_worktree_sets_needs_review(self, db, sample_project):
        """Missing worktree → task status set to needs-review."""
        from switchboard.dispatch.gates import _run_test_gate_inner

        task = await _make_task(db, worktree_path="/tmp/no-exist-xyz")
        project = await db.get_project("test-project")

        with patch("switchboard.dispatch.gates._run_test_streaming", AsyncMock(return_value=("", 0))):
            await _run_test_gate_inner("test-project/guard-task-1", project, task)

        refreshed = await db.get_task("test-project/guard-task-1")
        assert refreshed["status"] == "needs-review"

    async def test_missing_worktree_does_not_set_testing_status(self, db, sample_project):
        """Missing worktree → gate_status should not be set to 'testing'."""
        from switchboard.dispatch.gates import _run_test_gate_inner

        task = await _make_task(db, gate_status=None, worktree_path="/tmp/no-exist-xyz")
        project = await db.get_project("test-project")

        with patch("switchboard.dispatch.gates._run_test_streaming", AsyncMock(return_value=("", 0))):
            await _run_test_gate_inner("test-project/guard-task-1", project, task)

        refreshed = await db.get_task("test-project/guard-task-1")
        assert refreshed["gate_status"] != "testing"


# ---------------------------------------------------------------------------
# _dispatch_review_inner — missing worktree guard
# ---------------------------------------------------------------------------

class TestDispatchReviewInnerMissingWorktree:
    """_dispatch_review_inner returns early and sets needs-review when worktree is missing."""

    async def test_missing_worktree_returns_early(self, db, sample_project):
        """Missing worktree → review subtask is not dispatched."""
        from switchboard.dispatch.gates import _dispatch_review_inner

        task = await _make_task(db, worktree_path="/tmp/no-exist-xyz")
        project = await db.get_project("test-project")

        with patch("switchboard.dispatch.gates._run_subtask", AsyncMock()) as mock_subtask:
            await _dispatch_review_inner("test-project/guard-task-1", project, task)
            mock_subtask.assert_not_called()

    async def test_missing_worktree_sets_needs_review(self, db, sample_project):
        """Missing worktree → task status set to needs-review."""
        from switchboard.dispatch.gates import _dispatch_review_inner

        task = await _make_task(db, worktree_path="/tmp/no-exist-xyz")
        project = await db.get_project("test-project")

        with patch("switchboard.dispatch.gates._run_subtask", AsyncMock()):
            await _dispatch_review_inner("test-project/guard-task-1", project, task)

        refreshed = await db.get_task("test-project/guard-task-1")
        assert refreshed["status"] == "needs-review"


# ---------------------------------------------------------------------------
# retry_task gate re-entry — missing worktree falls through to normal retry
# ---------------------------------------------------------------------------

class TestRetryTaskGateReentryMissingWorktree:
    """retry_task skips gate re-entry when worktree is missing, falls through to normal retry."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mock_resume_pipeline = AsyncMock()
        self.mock_dispatch_task = AsyncMock(return_value={"status": "working"})
        self.mock_archive = AsyncMock()
        self.mock_invalidate = AsyncMock()
        self.mock_revert_punchlist = AsyncMock(return_value=0)

        patches = [
            patch("switchboard.dispatch.gates._resume_gate_pipeline", self.mock_resume_pipeline),
            patch("switchboard.dispatch.engine.dispatch_task", self.mock_dispatch_task),
            patch("switchboard.dispatch.engine.archive_task_logs", self.mock_archive),
            patch("switchboard.dispatch.engine._invalidate_chain", self.mock_invalidate),
            patch("switchboard.db.revert_punchlist_items_for_task", self.mock_revert_punchlist),
            patch("switchboard.db.write_audit_log", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_missing_worktree_skips_gate_reentry(self, db, sample_project):
        """When worktree is missing, gate re-entry is skipped even for interrupted gate states."""
        from switchboard.dispatch.engine import retry_task

        task = await _make_task(
            db, status="completed", gate_status="testing",
            worktree_path="/tmp/no-exist-xyz",
        )

        await retry_task("test-project/guard-task-1")

        # Gate pipeline should NOT be re-entered
        self.mock_resume_pipeline.assert_not_called()

    async def test_missing_worktree_falls_through_to_normal_retry(self, db, sample_project):
        """When worktree is missing, normal retry dispatch runs (which re-creates worktree)."""
        from switchboard.dispatch.engine import retry_task

        task = await _make_task(
            db, status="completed", gate_status="testing",
            worktree_path="/tmp/no-exist-xyz",
        )

        await retry_task("test-project/guard-task-1")

        # Normal dispatch should be called (which sets up a new worktree)
        self.mock_dispatch_task.assert_called_once()

    async def test_existing_worktree_uses_gate_reentry(self, db, sample_project, tmp_path):
        """When worktree exists and gate was interrupted, gate re-entry is used."""
        from switchboard.dispatch.engine import retry_task

        task = await _make_task(
            db, status="completed", gate_status="testing",
            worktree_path=str(tmp_path),
        )

        await retry_task("test-project/guard-task-1")

        # Gate pipeline should be re-entered
        self.mock_resume_pipeline.assert_called_once_with("test-project/guard-task-1", reason="retry")
        self.mock_dispatch_task.assert_not_called()


# ---------------------------------------------------------------------------
# _recover_gate_subtask — missing parent worktree
# ---------------------------------------------------------------------------

class TestRecoverGateSubtaskMissingWorktree:
    """_recover_gate_subtask handles missing parent worktree gracefully."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mock_run_test_gate = AsyncMock()
        self.mock_dispatch_review = AsyncMock()

        patches = [
            patch("switchboard.dispatch.gates._run_test_gate", self.mock_run_test_gate),
            patch("switchboard.dispatch.gates._dispatch_review", self.mock_dispatch_review),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_missing_parent_worktree_no_gate_triggered(self, db, sample_project):
        """Missing parent worktree → no test gate or review triggered."""
        from switchboard.dispatch.recovery import _recover_gate_subtask

        # Create parent task with missing worktree
        parent = await _make_task(
            db, task_id="test-project/parent-task",
            status="completed", gate_status="testing",
            worktree_path="/tmp/no-exist-xyz",
        )
        # Create subtask pointing to parent
        subtask = await _make_task(
            db, task_id="test-project/subtask-1",
            status="needs-review", parent_task_id="test-project/parent-task",
        )
        subtask_data = await db.get_task("test-project/subtask-1")

        await _recover_gate_subtask("test-project/subtask-1", subtask_data)

        self.mock_run_test_gate.assert_not_called()
        self.mock_dispatch_review.assert_not_called()

    async def test_missing_parent_worktree_sets_parent_needs_review(self, db, sample_project):
        """Missing parent worktree → parent task marked needs-review."""
        from switchboard.dispatch.recovery import _recover_gate_subtask

        parent = await _make_task(
            db, task_id="test-project/parent-task",
            status="completed", gate_status="testing",
            worktree_path="/tmp/no-exist-xyz",
        )
        subtask = await _make_task(
            db, task_id="test-project/subtask-1",
            status="needs-review", parent_task_id="test-project/parent-task",
        )
        subtask_data = await db.get_task("test-project/subtask-1")

        await _recover_gate_subtask("test-project/subtask-1", subtask_data)

        parent_refreshed = await db.get_task("test-project/parent-task")
        assert parent_refreshed["status"] == "needs-review"

    async def test_missing_parent_worktree_posts_message(self, db, sample_project):
        """Missing parent worktree → message posted to parent task thread."""
        from switchboard.dispatch.recovery import _recover_gate_subtask

        parent = await _make_task(
            db, task_id="test-project/parent-task",
            status="completed", gate_status="testing",
            worktree_path="/tmp/no-exist-xyz",
        )
        subtask = await _make_task(
            db, task_id="test-project/subtask-1",
            status="needs-review", parent_task_id="test-project/parent-task",
        )
        subtask_data = await db.get_task("test-project/subtask-1")

        await _recover_gate_subtask("test-project/subtask-1", subtask_data)

        thread = await db.read_task_messages("test-project/parent-task")
        messages = thread.get("messages", [])
        assert any("does not exist" in m.get("content", "") for m in messages)

    async def test_existing_parent_worktree_triggers_gate(self, db, sample_project, tmp_path):
        """Existing parent worktree + gate_status=testing → test gate re-triggered."""
        from switchboard.dispatch.recovery import _recover_gate_subtask

        parent = await _make_task(
            db, task_id="test-project/parent-task",
            status="completed", gate_status="testing",
            worktree_path=str(tmp_path),
        )
        subtask = await _make_task(
            db, task_id="test-project/subtask-1",
            status="needs-review", parent_task_id="test-project/parent-task",
        )
        subtask_data = await db.get_task("test-project/subtask-1")

        await _recover_gate_subtask("test-project/subtask-1", subtask_data)

        self.mock_run_test_gate.assert_called_once()
