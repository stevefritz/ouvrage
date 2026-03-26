"""Tests for FIFO task queue: queuing at concurrency limit, drain on completion."""

from unittest.mock import AsyncMock, patch

import pytest

import database as _db

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
        import tasks
        import database as _db

        # Fill all concurrency slots with working tasks
        max_concurrent = _db.DEFAULT_MAX_CONCURRENT
        for i in range(max_concurrent):
            t = await db.create_task(
                id=f"test-project/worker-{i}",
                project_id="test-project",
                goal=f"Worker {i}",
            )
            await db.update_task(t["id"], status="working")

        result = await tasks.dispatch_task(
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

    async def test_not_queued_when_slots_available(self, db, sample_project):
        """Task dispatches immediately when concurrency slots available."""
        import tasks

        # Mock SDK session to avoid actually running
        with patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()):
            result = await tasks.dispatch_task(
                project_id="test-project",
                task_id="test-project/immediate-task",
                goal="Should dispatch immediately",
            )

        assert result["queued"] is False
        assert result["status"] == "working"

    async def test_queued_response_includes_branch(self, db, sample_project):
        """Queued response includes the branch name."""
        import tasks

        for i in range(_MAX_CONCURRENT):
            t = await db.create_task(
                id=f"test-project/w-{i}", project_id="test-project", goal=f"W {i}",
            )
            await db.update_task(t["id"], status="working")

        result = await tasks.dispatch_task(
            project_id="test-project",
            task_id="test-project/queued-branch",
            goal="Queue with branch",
            branch="feature/my-branch",
        )

        assert result["queued"] is True
        assert result["branch"] == "feature/my-branch"

    async def test_depends_on_waiting_returns_queued_false(self, db, sample_project):
        """Task waiting on parent returns queued=false (it's waiting, not queued)."""
        import tasks

        parent = await db.create_task(
            id="test-project/parent",
            project_id="test-project",
            goal="Parent task",
        )

        result = await tasks.dispatch_task(
            project_id="test-project",
            task_id="test-project/child",
            goal="Child task",
            depends_on="test-project/parent",
        )

        assert result["status"] == "ready"
        assert result["queued"] is False
        assert result.get("waiting_on") == "test-project/parent"


# ---------------------------------------------------------------------------
# Queue drain on task completion
# ---------------------------------------------------------------------------

class TestQueueDrain:
    """_drain_queue dispatches oldest eligible task when slot opens."""

    async def test_drains_oldest_first(self, db, sample_project):
        """FIFO: oldest queued task dispatched first."""
        import tasks

        # Create 2 queued tasks with different queued_at
        t1 = await db.create_task(
            id="test-project/q1", project_id="test-project", goal="First",
        )
        await db.update_task(t1["id"], queued_at="2026-01-01T00:00:00Z")

        t2 = await db.create_task(
            id="test-project/q2", project_id="test-project", goal="Second",
        )
        await db.update_task(t2["id"], queued_at="2026-01-01T01:00:00Z")

        with patch("tasks.dispatch_task", AsyncMock()) as mock_dispatch:
            await tasks._drain_queue()
            mock_dispatch.assert_awaited_once()
            assert mock_dispatch.await_args.kwargs["task_id"] == "test-project/q1"

    async def test_no_drain_when_concurrency_full(self, db, sample_project):
        """Queue does not drain if concurrency slots are full."""
        import tasks

        # Fill concurrency
        for i in range(_MAX_CONCURRENT):
            t = await db.create_task(
                id=f"test-project/active-{i}", project_id="test-project", goal=f"Active {i}",
            )
            await db.update_task(t["id"], status="working")

        # Queue a task
        t = await db.create_task(
            id="test-project/waiting", project_id="test-project", goal="Waiting",
        )
        await db.update_task(t["id"], queued_at="2026-01-01T00:00:00Z")

        with patch("tasks.dispatch_task", AsyncMock()) as mock_dispatch:
            await tasks._drain_queue()
            mock_dispatch.assert_not_awaited()

    async def test_no_drain_when_queue_empty(self, db, sample_project):
        """Nothing happens when queue is empty."""
        import tasks

        with patch("tasks.dispatch_task", AsyncMock()) as mock_dispatch:
            await tasks._drain_queue()
            mock_dispatch.assert_not_awaited()

    async def test_skips_task_with_unfinished_depends(self, db, sample_project):
        """Queued task with unfinished depends_on is skipped."""
        import tasks

        parent = await db.create_task(
            id="test-project/parent", project_id="test-project", goal="Parent",
        )
        # Parent NOT gate-passed

        child = await db.create_task(
            id="test-project/child", project_id="test-project", goal="Child",
            depends_on="test-project/parent",
        )
        await db.update_task(child["id"], queued_at="2026-01-01T00:00:00Z")

        with patch("tasks.dispatch_task", AsyncMock()) as mock_dispatch:
            await tasks._drain_queue()
            mock_dispatch.assert_not_awaited()

    async def test_dispatches_when_depends_on_passed(self, db, sample_project):
        """Queued task with passed depends_on is eligible."""
        import tasks

        parent = await db.create_task(
            id="test-project/parent", project_id="test-project", goal="Parent",
        )
        await db.update_task(parent["id"], gate_passed_at=db.now_iso())

        child = await db.create_task(
            id="test-project/child", project_id="test-project", goal="Child",
            depends_on="test-project/parent",
        )
        await db.update_task(child["id"], queued_at="2026-01-01T00:00:00Z")

        with patch("tasks.dispatch_task", AsyncMock()) as mock_dispatch:
            await tasks._drain_queue()
            mock_dispatch.assert_awaited_once()
            assert mock_dispatch.await_args.kwargs["task_id"] == "test-project/child"


# ---------------------------------------------------------------------------
# Chain advancement takes priority over FIFO
# ---------------------------------------------------------------------------

class TestChainPriority:
    """depends_on advancement happens before FIFO queue drain."""

    async def test_chain_before_queue(self, db, sample_project):
        """When a task gate-passes, its dependent is dispatched BEFORE queued tasks."""
        import tasks

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

        async def mock_dispatch(**kwargs):
            dispatch_order.append(kwargs["task_id"])

        with patch("switchboard.dispatch.engine.dispatch_task", AsyncMock(side_effect=mock_dispatch)):
            with patch("switchboard.dispatch.engine._maybe_create_pr", AsyncMock()):
                with patch("switchboard.dispatch.engine._perform_auto_merge", AsyncMock(return_value=True)):
                    with patch("switchboard.dispatch.engine._auto_release_worktree", AsyncMock()):
                        await tasks._check_and_dispatch_dependents("test-project/parent")

        # Dependent dispatched first, then queue drain
        assert dispatch_order[0] == "test-project/dep"


# ---------------------------------------------------------------------------
# Mutual exclusion: auto_merge + auto_pr
# ---------------------------------------------------------------------------

class TestMutualExclusion:
    """auto_merge and auto_pr cannot both be true."""

    async def test_auto_merge_and_auto_pr_raises(self, db, sample_project):
        import tasks

        with pytest.raises(ValueError, match="mutually exclusive"):
            await tasks.dispatch_task(
                project_id="test-project",
                task_id="test-project/bad-combo",
                goal="Should fail",
                auto_merge=True,
                auto_pr=True,
            )

    async def test_auto_merge_alone_ok(self, db, sample_project):
        """auto_merge without auto_pr is fine."""
        import tasks

        # Fill concurrency so it queues (avoids SDK launch)
        for i in range(_MAX_CONCURRENT):
            t = await db.create_task(
                id=f"test-project/filler-{i}", project_id="test-project", goal=f"F {i}",
            )
            await db.update_task(t["id"], status="working")

        result = await tasks.dispatch_task(
            project_id="test-project",
            task_id="test-project/merge-only",
            goal="Merge only",
            auto_merge=True,
            auto_pr=False,
        )
        assert result["queued"] is True  # queued because concurrency full, but no error

    async def test_auto_pr_alone_ok(self, db, sample_project):
        """auto_pr without auto_merge is fine."""
        import tasks

        for i in range(_MAX_CONCURRENT):
            t = await db.create_task(
                id=f"test-project/filler2-{i}", project_id="test-project", goal=f"F {i}",
            )
            await db.update_task(t["id"], status="working")

        result = await tasks.dispatch_task(
            project_id="test-project",
            task_id="test-project/pr-only",
            goal="PR only",
            auto_merge=False,
            auto_pr=True,
        )
        assert result["queued"] is True


# ---------------------------------------------------------------------------
# get_queued_tasks helper
# ---------------------------------------------------------------------------

class TestGetQueuedTasks:
    """DB helper returns correct tasks in FIFO order."""

    async def test_returns_fifo_order(self, db, sample_project):
        t1 = await db.create_task(
            id="test-project/first", project_id="test-project", goal="First",
        )
        await db.update_task(t1["id"], queued_at="2026-01-01T00:00:00Z")

        t2 = await db.create_task(
            id="test-project/second", project_id="test-project", goal="Second",
        )
        await db.update_task(t2["id"], queued_at="2026-01-02T00:00:00Z")

        queued = await db.get_queued_tasks()
        assert len(queued) == 2
        assert queued[0]["id"] == "test-project/first"
        assert queued[1]["id"] == "test-project/second"

    async def test_excludes_non_ready(self, db, sample_project):
        t = await db.create_task(
            id="test-project/working", project_id="test-project", goal="Working",
        )
        await db.update_task(t["id"], status="working", queued_at="2026-01-01T00:00:00Z")

        queued = await db.get_queued_tasks()
        assert len(queued) == 0

    async def test_excludes_unfinished_depends(self, db, sample_project):
        parent = await db.create_task(
            id="test-project/parent", project_id="test-project", goal="Parent",
        )
        # Parent has no gate_passed_at

        child = await db.create_task(
            id="test-project/child", project_id="test-project", goal="Child",
            depends_on="test-project/parent",
        )
        await db.update_task(child["id"], queued_at="2026-01-01T00:00:00Z")

        queued = await db.get_queued_tasks()
        assert len(queued) == 0

    async def test_includes_passed_depends(self, db, sample_project):
        parent = await db.create_task(
            id="test-project/parent", project_id="test-project", goal="Parent",
        )
        await db.update_task(parent["id"], gate_passed_at=db.now_iso())

        child = await db.create_task(
            id="test-project/child", project_id="test-project", goal="Child",
            depends_on="test-project/parent",
        )
        await db.update_task(child["id"], queued_at="2026-01-01T00:00:00Z")

        queued = await db.get_queued_tasks()
        assert len(queued) == 1
        assert queued[0]["id"] == "test-project/child"
