"""Tests for v5 migration toolkit: expanded update_task, bulk_update_tasks, move_task."""

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def two_projects(db):
    """Two separate projects for cross-project validation tests."""
    p1 = await db.create_project(
        id="proj-alpha",
        repo="git@github.com:acme/alpha.git",
        working_dir="/work/alpha",
    )
    p2 = await db.create_project(
        id="proj-beta",
        repo="git@github.com:acme/beta.git",
        working_dir="/work/beta",
    )
    return p1, p2


@pytest.fixture
async def task_with_component(db, sample_project):
    """A task and a component in the same project."""
    task = await db.create_task(
        id="test-project/mig-task",
        project_id="test-project",
        goal="Migration test task",
    )
    comp = await db.create_component(
        id="mig-comp", project_id="test-project", name="Migration Component",
    )
    return task, comp


# ---------------------------------------------------------------------------
# update_task — individual field updates
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# update_task — component_id validation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# bulk_update_tasks
# ---------------------------------------------------------------------------

class TestBulkUpdateTasks:
    async def test_bulk_assign_component(self, db, sample_project):
        """Assign multiple tasks to a component in one call."""
        t1 = await db.create_task(id="test-project/bulk-1", project_id="test-project", goal="Bulk 1")
        t2 = await db.create_task(id="test-project/bulk-2", project_id="test-project", goal="Bulk 2")
        t3 = await db.create_task(id="test-project/bulk-3", project_id="test-project", goal="Bulk 3")
        comp = await db.create_component(id="bulk-comp", project_id="test-project", name="Bulk")

        count = await db.bulk_update_tasks(
            [t1["id"], t2["id"], t3["id"]],
            component_id=comp["id"],
        )
        assert count == 3

        for tid in [t1["id"], t2["id"], t3["id"]]:
            t = await db.get_task(tid)
            assert t["component_id"] == "bulk-comp"


    async def test_bulk_skips_nonexistent(self, db, sample_project):
        """Non-existent task IDs are skipped, count reflects only updated tasks."""
        t1 = await db.create_task(id="test-project/skip-1", project_id="test-project", goal="Skip 1")
        count = await db.bulk_update_tasks(
            [t1["id"], "test-project/does-not-exist"],
            model="opus",
        )
        assert count == 1

    async def test_bulk_update_tags(self, db, sample_project):
        t1 = await db.create_task(id="test-project/tag-1", project_id="test-project", goal="Tag 1")
        t2 = await db.create_task(id="test-project/tag-2", project_id="test-project", goal="Tag 2")

        await db.bulk_update_tasks([t1["id"], t2["id"]], tags=["chatbot", "v5"])

        for tid in [t1["id"], t2["id"]]:
            tags = await db.get_task_tags(tid)
            assert sorted(tags) == ["chatbot", "v5"]

    async def test_bulk_update_bad_component_skips(self, db, sample_project):
        """Bad component_id raises for each task — all get skipped."""
        t1 = await db.create_task(id="test-project/bcomp-1", project_id="test-project", goal="Bad Comp 1")
        count = await db.bulk_update_tasks([t1["id"]], component_id="nonexistent")
        assert count == 0


# ---------------------------------------------------------------------------
# move_task
# ---------------------------------------------------------------------------

class TestMoveTask:

    async def test_move_between_components(self, db, sample_project):
        """Move task from one component to another."""
        task = await db.create_task(id="test-project/movable", project_id="test-project", goal="Movable task")
        comp_a = await db.create_component(id="move-comp-a", project_id="test-project", name="Comp A")
        comp_b = await db.create_component(id="move-comp-b", project_id="test-project", name="Comp B")

        await db.move_task(task["id"], comp_a["id"])
        updated = await db.move_task(task["id"], comp_b["id"])
        assert updated["component_id"] == "move-comp-b"

    async def test_move_nonexistent_task_raises(self, db, sample_project):
        comp = await db.create_component(id="move-target", project_id="test-project", name="Target")
        with pytest.raises(ValueError, match="not found"):
            await db.move_task("test-project/ghost-task", comp["id"])

    async def test_move_nonexistent_component_raises(self, db, sample_task):
        with pytest.raises(ValueError, match="not found"):
            await db.move_task(sample_task["id"], "nonexistent-comp")

    async def test_move_cross_project_raises(self, db, two_projects):
        """Component from different project is rejected."""
        p1, p2 = two_projects
        task = await db.create_task(id="proj-alpha/cross-task", project_id="proj-alpha", goal="Cross task")
        comp = await db.create_component(id="beta-comp", project_id="proj-beta", name="Beta Comp")

        with pytest.raises(ValueError, match="proj-beta"):
            await db.move_task(task["id"], comp["id"])


# ---------------------------------------------------------------------------
# Schema — new columns exist
# ---------------------------------------------------------------------------

