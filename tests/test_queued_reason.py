"""Tests for queued reason determination in lifecycle.get_state_label().

Covers all four reason types:
  - dependency: depends_on set, parent not gate-passed
  - project_paused: project has paused=True
  - component_paused: component has paused=True
  - concurrency: fallback when none of the above apply
"""

import pytest

import ouvrage.db as db
from ouvrage.dispatch.lifecycle import TaskLifecycle, _determine_queued_reason

PROJECT_ID = "queued-reason-proj"
_lifecycle = TaskLifecycle()


async def _seed_project(db_mod, proj_id=PROJECT_ID):
    try:
        await db_mod.create_project(
            id=proj_id,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/qr-test",
        )
    except Exception:
        pass


async def _seed_task(db_mod, task_suffix, proj_id=PROJECT_ID, **extra):
    task_id = f"{proj_id}/{task_suffix}"
    await db_mod.create_task(id=task_id, project_id=proj_id, goal="queued reason test")
    if extra:
        await db_mod.update_task(task_id, **extra)
    return task_id


# ---------------------------------------------------------------------------
# _determine_queued_reason unit tests
# ---------------------------------------------------------------------------


class TestDetermineQueuedReason:
    """Direct unit tests for _determine_queued_reason()."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        await _seed_project(db)

    async def test_concurrency_fallback(self):
        """No depends_on, project/component not paused → concurrency."""
        task_id = await _seed_task(self.db, "conc-task", queued_at=db.now_iso())
        task = await self.db.get_task(task_id)
        reason, blocking_id = await _determine_queued_reason(task)
        assert reason == "concurrency"
        assert blocking_id is None

    async def test_dependency_reason(self):
        """depends_on set and parent not gate-passed → dependency."""
        parent_id = await _seed_task(self.db, "parent-task")
        child_id = await _seed_task(
            self.db, "child-task",
            queued_at=db.now_iso(), depends_on=parent_id,
        )
        task = await self.db.get_task(child_id)
        reason, blocking_id = await _determine_queued_reason(task)
        assert reason == "dependency"
        assert blocking_id == parent_id

    async def test_dependency_cleared_when_parent_gate_passed(self):
        """depends_on set but parent has gate_passed_at → NOT dependency → concurrency."""
        parent_id = await _seed_task(
            self.db, "gated-parent",
            status="completed", gate_passed_at=db.now_iso(),
        )
        child_id = await _seed_task(
            self.db, "gated-child",
            queued_at=db.now_iso(), depends_on=parent_id,
        )
        task = await self.db.get_task(child_id)
        reason, blocking_id = await _determine_queued_reason(task)
        assert reason == "concurrency"
        assert blocking_id is None

    async def test_project_paused(self):
        """Project is paused → project_paused reason."""
        await self.db.update_project(PROJECT_ID, paused=True)
        try:
            task_id = await _seed_task(self.db, "pp-task", queued_at=db.now_iso())
            task = await self.db.get_task(task_id)
            reason, blocking_id = await _determine_queued_reason(task)
            assert reason == "project_paused"
            assert blocking_id is None
        finally:
            await self.db.update_project(PROJECT_ID, paused=False)

    async def test_component_paused(self):
        """Component is paused → component_paused reason."""
        comp = await self.db.create_component(
            id="qr-comp",
            project_id=PROJECT_ID,
            name="qr-comp",
        )
        comp_id = comp["id"]
        await self.db.update_component(comp_id, paused=True)
        task_id = await _seed_task(
            self.db, "cp-task",
            queued_at=db.now_iso(), component_id=comp_id,
        )
        task = await self.db.get_task(task_id)
        reason, blocking_id = await _determine_queued_reason(task)
        assert reason == "component_paused"
        assert blocking_id is None

    async def test_dependency_takes_priority_over_project_paused(self):
        """depends_on unmet takes priority over project being paused."""
        await self.db.update_project(PROJECT_ID, paused=True)
        try:
            parent_id = await _seed_task(self.db, "dep-pri-parent")
            child_id = await _seed_task(
                self.db, "dep-pri-child",
                queued_at=db.now_iso(), depends_on=parent_id,
            )
            task = await self.db.get_task(child_id)
            reason, blocking_id = await _determine_queued_reason(task)
            assert reason == "dependency"
            assert blocking_id == parent_id
        finally:
            await self.db.update_project(PROJECT_ID, paused=False)


# ---------------------------------------------------------------------------
# get_state_label integration tests
# ---------------------------------------------------------------------------


class TestGetStateLabelQueuedReason:
    """get_state_label() returns queued_reason when task is queued."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        await _seed_project(db)

    async def test_queued_reason_absent_for_non_queued_task(self):
        """Non-queued tasks have queued_reason=None."""
        task_id = await _seed_task(self.db, "working-task", status="working")
        info = await _lifecycle.get_state_label(task_id)
        assert info["queued_reason"] is None
        assert info["queued_blocking_task_id"] is None

    async def test_queued_reason_concurrency(self):
        """Queued task with no blocking cause → concurrency."""
        task_id = await _seed_task(self.db, "sl-conc", queued_at=db.now_iso())
        info = await _lifecycle.get_state_label(task_id)
        assert info["reason"] == "queued"
        assert info["queued_reason"] == "concurrency"
        assert info["queued_blocking_task_id"] is None

    async def test_queued_reason_dependency(self):
        """Queued task with unmet depends_on → dependency + blocking task ID."""
        parent_id = await _seed_task(self.db, "sl-parent")
        child_id = await _seed_task(
            self.db, "sl-child",
            queued_at=db.now_iso(), depends_on=parent_id,
        )
        info = await _lifecycle.get_state_label(child_id)
        assert info["reason"] == "queued"
        assert info["queued_reason"] == "dependency"
        assert info["queued_blocking_task_id"] == parent_id

    async def test_queued_reason_project_paused(self):
        """Queued task whose project is paused → project_paused."""
        await self.db.update_project(PROJECT_ID, paused=True)
        try:
            task_id = await _seed_task(self.db, "sl-proj-paused", queued_at=db.now_iso())
            info = await _lifecycle.get_state_label(task_id)
            assert info["reason"] == "queued"
            assert info["queued_reason"] == "project_paused"
        finally:
            await self.db.update_project(PROJECT_ID, paused=False)

    async def test_queued_reason_keys_always_present(self):
        """queued_reason and queued_blocking_task_id always present in state label response."""
        task_id = await _seed_task(self.db, "sl-keys", status="working")
        info = await _lifecycle.get_state_label(task_id)
        assert "queued_reason" in info
        assert "queued_blocking_task_id" in info
