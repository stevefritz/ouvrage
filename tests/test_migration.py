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

class TestUpdateTask:
    async def test_update_component_id(self, db, task_with_component):
        task, comp = task_with_component
        updated = await db.update_task(task["id"], component_id=comp["id"])
        assert updated["component_id"] == comp["id"]

    async def test_update_base_branch(self, db, sample_task):
        updated = await db.update_task(sample_task["id"], base_branch="feature/v2")
        assert updated["base_branch"] == "feature/v2"

    async def test_update_branch_target(self, db, sample_task):
        updated = await db.update_task(sample_task["id"], branch_target="main")
        assert updated["branch_target"] == "main"

    async def test_update_tags_replace(self, db, sample_task):
        await db.update_task(sample_task["id"], tags=["alpha", "beta"])
        updated = await db.update_task(sample_task["id"], tags=["gamma"])
        assert updated["tags"] == ["gamma"]

    async def test_update_tags_add(self, db, sample_task):
        updated = await db.update_task(sample_task["id"], tags=["bugfix", "review"])
        assert sorted(updated["tags"]) == ["bugfix", "review"]

    async def test_update_auto_test(self, db, sample_task):
        updated = await db.update_task(sample_task["id"], auto_test=False)
        assert updated["auto_test"] == 0 or updated["auto_test"] is False

    async def test_update_auto_review(self, db, sample_task):
        updated = await db.update_task(sample_task["id"], auto_review=False)
        assert updated["auto_review"] == 0 or updated["auto_review"] is False

    async def test_update_auto_merge(self, db, sample_task):
        updated = await db.update_task(sample_task["id"], auto_merge=True)
        assert updated["auto_merge"] == 1 or updated["auto_merge"] is True

    async def test_update_auto_pr(self, db, sample_task):
        updated = await db.update_task(sample_task["id"], auto_pr=True)
        assert updated["auto_pr"] == 1 or updated["auto_pr"] is True

    async def test_update_max_test_retries(self, db, sample_task):
        updated = await db.update_task(sample_task["id"], max_test_retries=5)
        assert updated["max_test_retries"] == 5

    async def test_update_max_review_retries(self, db, sample_task):
        updated = await db.update_task(sample_task["id"], max_review_retries=2)
        assert updated["max_review_retries"] == 2

    async def test_update_model(self, db, sample_task):
        updated = await db.update_task(sample_task["id"], model="opus")
        assert updated["model"] == "opus"

    async def test_update_jira_ticket(self, db, sample_task):
        updated = await db.update_task(sample_task["id"], jira_ticket="SUZY-999")
        assert updated["jira_ticket"] == "SUZY-999"

    async def test_update_conversation_id(self, db, sample_task):
        updated = await db.update_task(sample_task["id"], conversation_id="widget-redesign")
        assert updated["conversation_id"] == "widget-redesign"

    async def test_update_claude_chat_url(self, db, sample_task):
        url = "https://claude.ai/chat/abc123"
        updated = await db.update_task(sample_task["id"], claude_chat_url=url)
        assert updated["claude_chat_url"] == url

    async def test_update_returns_tags(self, db, sample_task):
        """update_task always returns tags list (even when not updating tags)."""
        updated = await db.update_task(sample_task["id"], model="sonnet")
        assert "tags" in updated
        assert isinstance(updated["tags"], list)

    async def test_update_nonexistent_task_raises(self, db, sample_project):
        with pytest.raises(ValueError, match="not found"):
            await db.update_task("test-project/does-not-exist", model="opus")


# ---------------------------------------------------------------------------
# update_task — component_id validation
# ---------------------------------------------------------------------------

class TestUpdateTaskComponentValidation:
    async def test_bad_component_id_raises(self, db, sample_task):
        with pytest.raises(ValueError, match="not found"):
            await db.update_task(sample_task["id"], component_id="nonexistent-comp")

    async def test_null_component_id_clears(self, db, task_with_component):
        task, comp = task_with_component
        await db.update_task(task["id"], component_id=comp["id"])
        updated = await db.update_task(task["id"], component_id=None)
        assert updated["component_id"] is None


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

    async def test_bulk_update_returns_count(self, db, sample_project):
        t1 = await db.create_task(id="test-project/cnt-1", project_id="test-project", goal="Count 1")
        t2 = await db.create_task(id="test-project/cnt-2", project_id="test-project", goal="Count 2")

        count = await db.bulk_update_tasks([t1["id"], t2["id"]], model="opus")
        assert count == 2

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

    async def test_bulk_empty_list(self, db, sample_project):
        count = await db.bulk_update_tasks([], model="opus")
        assert count == 0


# ---------------------------------------------------------------------------
# move_task
# ---------------------------------------------------------------------------

class TestMoveTask:
    async def test_move_to_component(self, db, task_with_component):
        task, comp = task_with_component
        updated = await db.move_task(task["id"], comp["id"])
        assert updated["component_id"] == comp["id"]

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

    async def test_move_returns_updated_task(self, db, task_with_component):
        task, comp = task_with_component
        result = await db.move_task(task["id"], comp["id"])
        assert result["id"] == task["id"]
        assert result["component_id"] == comp["id"]
        assert "tags" in result


# ---------------------------------------------------------------------------
# Schema — new columns exist
# ---------------------------------------------------------------------------

class TestNewTaskColumns:
    async def test_new_columns_exist(self, db, sample_task):
        """Verify all new v5 migration columns are present in task rows."""
        task = await db.get_task(sample_task["id"])
        for col in ("base_branch", "branch_target", "claude_chat_url", "auto_merge",
                    "max_test_retries", "max_review_retries"):
            assert col in task, f"Missing column: {col}"

    async def test_new_columns_default_null(self, db, sample_task):
        task = await db.get_task(sample_task["id"])
        assert task["base_branch"] is None
        assert task["branch_target"] is None
        assert task["claude_chat_url"] is None
        assert task["auto_merge"] is None
        assert task["max_test_retries"] is None
        assert task["max_review_retries"] is None
