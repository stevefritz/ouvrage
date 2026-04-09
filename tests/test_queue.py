"""Tests for FIFO task queue: queuing at concurrency limit, drain on completion."""

from unittest.mock import AsyncMock, patch

import pytest

import switchboard.db as _db
from switchboard.dispatch.engine import dispatch_task, _check_and_dispatch_dependents
from switchboard.dispatch.queue import _drain_queue

# Use actual concurrency limit from the database module
_MAX_CONCURRENT = _db.DEFAULT_MAX_CONCURRENT


# ---------------------------------------------------------------------------
# dispatch_task queuing behavior
# ---------------------------------------------------------------------------

class TestDispatchTaskQueuing:
    """dispatch_task creates task and queues when concurrency is full."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_setup_worktree = AsyncMock(return_value="/tmp/fake-worktree")
        self.mock_run_setup = AsyncMock()
        self.mock_notify = AsyncMock()

        patches = [
            patch("switchboard.dispatch.engine.setup_worktree", self.mock_setup_worktree),
            patch("switchboard.dispatch.engine.run_setup_command", self.mock_run_setup),
            patch("switchboard.dispatch.engine.notify", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_queued_at_concurrency_limit(self, db, sample_project):
        """Task is created with queued_at when concurrency is full."""
        # Fill all concurrency slots with working tasks
        max_concurrent = _db.DEFAULT_MAX_CONCURRENT
        for i in range(max_concurrent):
            t = await db.create_task(
                id=f"test-project/worker-{i}",
                project_id="test-project",
                goal=f"Worker {i}",
            )
            await db.update_task(t["id"], status="working")

        result = await dispatch_task(
            project_id="test-project",
            task_id="test-project/queued-task",
            goal="Should be queued",
        )

        assert result["queued"] is True
        assert result["status"] == "ready"
        assert "queued_at" in result

        # Task exists in DB with queued_at set
        task = await db.get_task("test-project/queued-task")
        assert task["status"] == "ready"
        assert task["queued_at"] is not None


# ---------------------------------------------------------------------------
# Queue drain on task completion
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Chain advancement takes priority over FIFO
# ---------------------------------------------------------------------------

class TestChainPriority:
    """depends_on advancement happens before FIFO queue drain."""

    async def test_chain_before_queue(self, db, sample_project):
        """When a task gate-passes, its dependent is dispatched BEFORE queued tasks."""
        # Parent task that just gate-passed
        parent = await db.create_task(
            id="test-project/parent", project_id="test-project", goal="Parent",
        )
        await db.update_task(parent["id"],
            status="completed", gate_status="passed", gate_passed_at=db.now_iso(),
        )

        # Dependent waiting on parent
        dep = await db.create_task(
            id="test-project/dep", project_id="test-project", goal="Dependent",
            depends_on="test-project/parent",
        )

        # FIFO queued task (queued before dep)
        queued = await db.create_task(
            id="test-project/fifo", project_id="test-project", goal="FIFO task",
        )
        await db.update_task(queued["id"], queued_at="2025-01-01T00:00:00Z")

        dispatch_order = []

        original_execute = None
        async def mock_execute(task_id, action, **ctx):
            dispatch_order.append(task_id)

        with patch("switchboard.dispatch.lifecycle.lifecycle.execute", AsyncMock(side_effect=mock_execute)):
            with patch("switchboard.dispatch.engine._maybe_create_pr", AsyncMock()):
                with patch("switchboard.dispatch.engine._perform_auto_merge", AsyncMock(return_value=True)):
                    with patch("switchboard.dispatch.engine._auto_release_worktree", AsyncMock()):
                        await _check_and_dispatch_dependents("test-project/parent")

        # Dependent dispatched first, then queue drain
        assert dispatch_order[0] == "test-project/dep"


# ---------------------------------------------------------------------------
# Mutual exclusion: auto_merge + auto_pr
# ---------------------------------------------------------------------------

class TestMutualExclusion:
    """auto_merge and auto_pr cannot both be true."""

    async def test_auto_merge_and_auto_pr_raises(self, db, sample_project):
        with pytest.raises(ValueError, match="mutually exclusive"):
            await dispatch_task(
                project_id="test-project",
                task_id="test-project/bad-combo",
                goal="Should fail",
                auto_merge=True,
                auto_pr=True,
            )

    async def test_auto_merge_alone_ok(self, db, sample_project):
        """auto_merge without auto_pr is fine."""
        # Fill concurrency so it queues (avoids SDK launch)
        for i in range(_MAX_CONCURRENT):
            t = await db.create_task(
                id=f"test-project/filler-{i}", project_id="test-project", goal=f"F {i}",
            )
            await db.update_task(t["id"], status="working")

        result = await dispatch_task(
            project_id="test-project",
            task_id="test-project/merge-only",
            goal="Merge only",
            auto_merge=True,
            auto_pr=False,
        )
        assert result["queued"] is True  # queued because concurrency full, but no error


# ---------------------------------------------------------------------------
# get_queued_tasks helper
# ---------------------------------------------------------------------------

