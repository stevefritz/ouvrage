"""Tests for v5 components: CRUD, config inheritance, conversations, dispatch integration."""

from unittest.mock import AsyncMock, patch
import pytest


# ---------------------------------------------------------------------------
# Component CRUD
# ---------------------------------------------------------------------------

class TestComponentCRUD:


    async def test_create_component_bad_project(self, db):
        with pytest.raises(ValueError, match="not found"):
            await db.create_component(
                id="orphan", project_id="nonexistent", name="Orphan",
            )


    async def test_get_component_not_found(self, db):
        assert await db.get_component("nope") is None

    async def test_dead_fields_absent_from_responses(self, db, sample_project):
        """env_overrides and secrets must not appear in component API responses."""
        await db.create_component(
            id="dead-fields", project_id="test-project", name="Dead Fields",
        )
        comp = await db.get_component("dead-fields")
        assert "env_overrides" not in comp
        assert "secrets" not in comp

        updated = await db.update_component("dead-fields", name="Dead Fields 2")
        assert "env_overrides" not in updated
        assert "secrets" not in updated

        comps = await db.list_components(project_id="test-project")
        for c in comps:
            assert "env_overrides" not in c
            assert "secrets" not in c


    async def test_update_component_review_ignore_patterns(self, db, sample_project):
        await db.create_component(
            id="patterns-test", project_id="test-project", name="Patterns",
        )
        updated = await db.update_component("patterns-test", review_ignore_patterns=["*.lock", "vendor/"])
        assert updated["review_ignore_patterns"] == ["*.lock", "vendor/"]

    async def test_update_component_not_found(self, db):
        with pytest.raises(ValueError, match="not found"):
            await db.update_component("ghost", name="Nope")

    async def test_list_components(self, db, sample_project):
        await db.create_component(id="comp-a", project_id="test-project", name="A")
        await db.create_component(id="comp-b", project_id="test-project", name="B")
        comps = await db.list_components()
        assert len(comps) == 2


    async def test_get_component_task_summary(self, db, sample_project):
        await db.create_component(id="summarized", project_id="test-project", name="Summarized")
        t1 = await db.create_task(
            id="test-project/s1", project_id="test-project", goal="S1",
            component_id="summarized",
        )
        await db.update_task(t1["id"], status="working")
        await db.create_task(
            id="test-project/s2", project_id="test-project", goal="S2",
            component_id="summarized",
        )
        comp = await db.get_component("summarized")
        assert comp["task_summary"]["total"] == 2
        assert comp["task_summary"]["active"] == 1
        assert comp["task_summary"]["by_status"]["working"] == 1
        assert comp["task_summary"]["by_status"]["ready"] == 1


# ---------------------------------------------------------------------------
# Component Conversations
# ---------------------------------------------------------------------------

class TestComponentConversations:

    async def test_unlink_conversation(self, db, sample_project, sample_conversation):
        await db.create_component(id="unlinked", project_id="test-project", name="Unlinked")
        await db.link_conversation("unlinked", "widget-redesign")
        result = await db.unlink_conversation("unlinked", "widget-redesign")
        assert result["unlinked"] is True

        convs = await db.get_component_conversations("unlinked")
        assert convs == []


    async def test_get_component_includes_conversations(self, db, sample_project, sample_conversation):
        await db.create_component(id="with-conv", project_id="test-project", name="With Conv")
        await db.link_conversation("with-conv", "widget-redesign")
        comp = await db.get_component("with-conv")
        conv_ids = [c["id"] for c in comp["conversations"]]
        assert "widget-redesign" in conv_ids


# ---------------------------------------------------------------------------
# Config Inheritance
# ---------------------------------------------------------------------------

class TestConfigInheritance:


    async def test_resolve_config_not_found(self, db):
        with pytest.raises(ValueError, match="not found"):
            await db.resolve_config("nonexistent")


# ---------------------------------------------------------------------------
# list_tasks with component_id filter
# ---------------------------------------------------------------------------

class TestListTasksComponentFilter:
    async def test_list_tasks_by_component(self, db, sample_project):
        await db.create_component(id="filter-comp", project_id="test-project", name="Filter")
        await db.create_task(
            id="test-project/in-comp", project_id="test-project",
            goal="In component", component_id="filter-comp",
        )
        await db.create_task(
            id="test-project/no-comp", project_id="test-project",
            goal="No component",
        )
        tasks = await db.list_tasks(component_id="filter-comp")
        assert len(tasks) == 1
        assert tasks[0]["id"] == "test-project/in-comp"


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Project config fields: create/update/resolve
# ---------------------------------------------------------------------------

class TestProjectConfigFields:


    async def test_project_auto_pr_falls_through_to_task(self, db):
        """project.auto_pr=True is stored as resolved value when dispatch_task has no explicit auto_pr."""
        await db.create_project(
            id="autopr-proj", repo="https://github.com/x/y.git",
            working_dir="/work/autopr-proj",
        )
        await db.update_project("autopr-proj", auto_pr=True)

        # Use dispatch_task with concurrency full so it creates-but-queues the task.
        # This exercises the config resolution path before create_task.
        import switchboard.db as _db
        for i in range(_db.DEFAULT_MAX_CONCURRENT):
            t = await db.create_task(
                id=f"autopr-proj/filler-{i}", project_id="autopr-proj", goal=f"Filler {i}",
            )
            await db.update_task(t["id"], status="working")

        from switchboard.dispatch.engine import dispatch_task
        with patch("switchboard.dispatch.engine.notify", AsyncMock()):
            result = await dispatch_task(
                project_id="autopr-proj",
                task_id="autopr-proj/task1",
                goal="Test project auto_pr inheritance",
            )

        assert result["queued"] is True
        task = await db.get_task("autopr-proj/task1")
        # auto_pr should have been resolved from project (True) since none was passed
        assert task["auto_pr"] == 1 or task["auto_pr"] is True


# ---------------------------------------------------------------------------
# update_task: new settable fields
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# max_test_retries / max_review_retries gate wiring
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# dispatch_task: boolean schema fix — auto_release_worktree resolves via engine
# ---------------------------------------------------------------------------

class TestAutoReleaseWorktreeResolution:
    @pytest.fixture(autouse=True)
    def mock_git(self):
        with patch("switchboard.dispatch.engine._run_as_worker") as mock_worker, \
             patch("switchboard.dispatch.engine.setup_worktree") as mock_setup, \
             patch("switchboard.dispatch.engine.cleanup_worktree") as mock_cleanup:
            mock_setup.return_value = "/tmp/fake-worktree"
            mock_worker.return_value = None
            mock_cleanup.return_value = None
            yield


# ---------------------------------------------------------------------------
# create_component: review_ignore_patterns settable at creation
# ---------------------------------------------------------------------------

class TestCreateComponentReviewIgnorePatterns:
    async def test_review_ignore_patterns_at_creation(self, db, sample_project):
        """review_ignore_patterns can be set when creating a component."""
        comp = await db.create_component(
            id="ignore-comp", project_id="test-project", name="Test",
            review_ignore_patterns=["*.lock", "vendor/"],
        )
        assert comp["review_ignore_patterns"] == ["*.lock", "vendor/"]
        fetched = await db.get_component("ignore-comp")
        assert fetched["review_ignore_patterns"] == ["*.lock", "vendor/"]

