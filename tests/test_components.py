"""Tests for v5 components: CRUD, config inheritance, conversations, dispatch integration."""

from unittest.mock import AsyncMock, patch
import pytest


# ---------------------------------------------------------------------------
# Component CRUD
# ---------------------------------------------------------------------------

class TestComponentCRUD:
    async def test_create_component_basic(self, db, sample_project):
        comp = await db.create_component(
            id="widget-sort", project_id="test-project", name="Widget Sorting",
        )
        assert comp["id"] == "widget-sort"
        assert comp["project_id"] == "test-project"
        assert comp["name"] == "Widget Sorting"
        assert comp["phase"] == "planning"

    async def test_create_component_with_description_and_phase(self, db, sample_project):
        comp = await db.create_component(
            id="full-config", project_id="test-project", name="Full Config",
            description="Everything set",
            phase="building",
        )
        assert comp["description"] == "Everything set"
        assert comp["phase"] == "building"

    async def test_create_component_ignores_dead_config_fields(self, db, sample_project):
        """Dead config fields (model, max_turns, env_overrides, etc.) are silently ignored."""
        comp = await db.create_component(
            id="ignore-config", project_id="test-project", name="Ignore Config",
            model="opus", max_turns=100, env_overrides={"DB": "sqlite"},
        )
        assert comp["id"] == "ignore-config"
        # Dead fields are not present in return value
        assert "model" not in comp or comp.get("model") is None
        assert "max_turns" not in comp or comp.get("max_turns") is None

    async def test_create_component_bad_project(self, db):
        with pytest.raises(ValueError, match="not found"):
            await db.create_component(
                id="orphan", project_id="nonexistent", name="Orphan",
            )

    async def test_get_component(self, db, sample_project):
        await db.create_component(
            id="get-me", project_id="test-project", name="Get Me",
        )
        comp = await db.get_component("get-me")
        assert comp is not None
        assert comp["name"] == "Get Me"
        assert comp["task_summary"]["total"] == 0
        assert comp["conversations"] == []

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

    async def test_update_component(self, db, sample_project):
        await db.create_component(
            id="updatable", project_id="test-project", name="Before",
        )
        updated = await db.update_component("updatable", name="After", phase="building")
        assert updated["name"] == "After"
        assert updated["phase"] == "building"

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

    async def test_list_components_by_project(self, db, sample_project):
        await db.create_component(id="comp-x", project_id="test-project", name="X")
        # Create another project
        await db.create_project(id="other", repo="git@github.com:acme/other.git", working_dir="/work/other")
        await db.create_component(id="comp-y", project_id="other", name="Y")
        comps = await db.list_components(project_id="test-project")
        assert len(comps) == 1
        assert comps[0]["id"] == "comp-x"

    async def test_list_components_includes_task_count(self, db, sample_project):
        await db.create_component(id="counted", project_id="test-project", name="Counted")
        await db.create_task(
            id="test-project/t1", project_id="test-project", goal="Task 1",
            component_id="counted",
        )
        await db.create_task(
            id="test-project/t2", project_id="test-project", goal="Task 2",
            component_id="counted",
        )
        comps = await db.list_components(project_id="test-project")
        assert comps[0]["total_tasks"] == 2
        assert comps[0]["active_tasks"] == 0
        assert comps[0]["done_tasks"] == 0
        assert comps[0]["conversation_count"] == 0
        assert comps[0]["open_punchlist"] == 0

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
    async def test_link_conversation(self, db, sample_project, sample_conversation):
        await db.create_component(id="linked", project_id="test-project", name="Linked")
        result = await db.link_conversation("linked", "widget-redesign")
        assert result["linked"] is True

        convs = await db.get_component_conversations("linked")
        assert "widget-redesign" in convs

    async def test_unlink_conversation(self, db, sample_project, sample_conversation):
        await db.create_component(id="unlinked", project_id="test-project", name="Unlinked")
        await db.link_conversation("unlinked", "widget-redesign")
        result = await db.unlink_conversation("unlinked", "widget-redesign")
        assert result["unlinked"] is True

        convs = await db.get_component_conversations("unlinked")
        assert convs == []

    async def test_link_duplicate_is_idempotent(self, db, sample_project, sample_conversation):
        await db.create_component(id="duped", project_id="test-project", name="Duped")
        await db.link_conversation("duped", "widget-redesign")
        await db.link_conversation("duped", "widget-redesign")  # no error
        convs = await db.get_component_conversations("duped")
        assert len(convs) == 1

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
    async def test_task_overrides_project(self, db, sample_project):
        """Task value > project value."""
        task = await db.create_task(
            id="test-project/override", project_id="test-project",
            goal="Override test",
            model="sonnet",  # task overrides project's "opus"
        )
        resolved = await db.resolve_config(task["id"])
        assert resolved["model"] == "sonnet"  # task wins
        assert resolved["max_turns"] == 150  # from project (task has none)

    async def test_null_task_falls_through_to_project(self, db, sample_project):
        """When task has no value, project's value is used."""
        # sample_project has model="opus", max_turns=150
        task = await db.create_task(
            id="test-project/fallthrough", project_id="test-project",
            goal="Fallthrough",
        )
        resolved = await db.resolve_config(task["id"])
        assert resolved["model"] == "opus"  # from project
        assert resolved["max_turns"] == 150  # from project
        assert resolved["test_command"] == "python -m pytest tests/ -v"  # from project

    async def test_falls_through_to_system_defaults(self, db, sample_project):
        """When neither task nor project set a value, system defaults are used."""
        task = await db.create_task(
            id="test-project/defaults-task", project_id="test-project",
            goal="Defaults",
        )
        resolved = await db.resolve_config(task["id"])
        assert resolved["auto_test"] is True  # system default
        assert resolved["auto_review"] is True  # system default
        assert resolved["review_model"] == "opus"  # system default
        assert resolved["max_test_retries"] == 3  # system default
        assert resolved["max_review_retries"] == 2  # system default
        assert resolved["auto_pr"] is False  # system default
        assert resolved["auto_merge"] is False  # system default

    async def test_env_overrides_from_project(self, db, sample_project):
        """env_overrides come from project level."""
        # sample_project has env_overrides={"NODE_ENV": "test", "DEBUG": "1"}
        task = await db.create_task(
            id="test-project/env-task", project_id="test-project",
            goal="Env task",
        )
        resolved = await db.resolve_config(task["id"])
        env = resolved["env_overrides"]
        assert env["NODE_ENV"] == "test"
        assert env["DEBUG"] == "1"

    async def test_task_with_component_still_resolves(self, db, sample_project):
        """Tasks with a component_id resolve from task → project → defaults (no component layer)."""
        await db.create_component(
            id="org-comp", project_id="test-project", name="Org Component",
        )
        task = await db.create_task(
            id="test-project/comp-task", project_id="test-project",
            goal="With component", component_id="org-comp",
        )
        resolved = await db.resolve_config(task["id"])
        assert resolved["model"] == "opus"  # from project
        assert resolved["max_turns"] == 150  # from project

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

    async def test_list_tasks_without_filter_returns_all(self, db, sample_project):
        await db.create_component(id="all-comp", project_id="test-project", name="All")
        await db.create_task(
            id="test-project/with", project_id="test-project",
            goal="With", component_id="all-comp",
        )
        await db.create_task(
            id="test-project/without", project_id="test-project",
            goal="Without",
        )
        tasks = await db.list_tasks()
        assert len(tasks) == 2


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

class TestComponentSchema:
    async def test_components_table_exists(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='components'"
            )
            assert len(rows) == 1

    async def test_component_conversations_table_exists(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='component_conversations'"
            )
            assert len(rows) == 1

    async def test_tasks_has_component_id_column(self, db):
        async with db.get_db() as conn:
            cols = await conn.execute_fetchall("PRAGMA table_info(tasks)")
            col_names = [c["name"] for c in cols]
            assert "component_id" in col_names

    async def test_projects_has_connectors_column(self, db):
        async with db.get_db() as conn:
            cols = await conn.execute_fetchall("PRAGMA table_info(projects)")
            col_names = [c["name"] for c in cols]
            assert "connectors" in col_names

    async def test_projects_has_new_config_columns(self, db):
        """Projects table should have review_model and boolean config columns."""
        async with db.get_db() as conn:
            cols = await conn.execute_fetchall("PRAGMA table_info(projects)")
            col_names = [c["name"] for c in cols]
            for col in ("review_model", "review_ignore_patterns", "auto_test", "auto_review", "auto_pr", "auto_merge"):
                assert col in col_names, f"Missing column: {col}"


# ---------------------------------------------------------------------------
# Project config fields: create/update/resolve
# ---------------------------------------------------------------------------

class TestProjectConfigFields:
    async def test_create_project_with_review_model(self, db):
        p = await db.create_project(
            id="rmodel-proj", repo="https://github.com/x/y.git",
            working_dir="/work/rmodel", review_model="sonnet",
        )
        assert p["review_model"] == "sonnet"
        fetched = await db.get_project("rmodel-proj")
        assert fetched["review_model"] == "sonnet"

    async def test_create_project_with_review_ignore_patterns(self, db):
        p = await db.create_project(
            id="rip-proj", repo="https://github.com/x/y.git",
            working_dir="/work/rip",
            review_ignore_patterns=["*.lock", "vendor/"],
        )
        assert p["review_ignore_patterns"] == ["*.lock", "vendor/"]
        fetched = await db.get_project("rip-proj")
        assert fetched["review_ignore_patterns"] == ["*.lock", "vendor/"]

    async def test_update_project_model(self, db):
        """model field is settable on update_project."""
        await db.create_project(
            id="upd-model", repo="https://github.com/x/y.git", working_dir="/work/upd-model",
        )
        updated = await db.update_project("upd-model", model="opus")
        assert updated["model"] == "opus"

    async def test_update_project_review_model(self, db):
        await db.create_project(
            id="upd-rmodel", repo="https://github.com/x/y.git", working_dir="/work/upd-rmodel",
        )
        updated = await db.update_project("upd-rmodel", review_model="sonnet")
        assert updated["review_model"] == "sonnet"

    async def test_update_project_review_ignore_patterns(self, db):
        await db.create_project(
            id="upd-rip", repo="https://github.com/x/y.git", working_dir="/work/upd-rip",
        )
        updated = await db.update_project("upd-rip", review_ignore_patterns=["*.log"])
        assert updated["review_ignore_patterns"] == ["*.log"]

    async def test_update_project_auto_pr(self, db):
        await db.create_project(
            id="upd-autopr", repo="https://github.com/x/y.git", working_dir="/work/upd-autopr",
        )
        updated = await db.update_project("upd-autopr", auto_pr=True)
        assert updated["auto_pr"] == 1 or updated["auto_pr"] is True

    async def test_project_review_model_falls_through_to_task(self, db):
        """project.review_model is used when task has no review_model."""
        p = await db.create_project(
            id="rm-inherit", repo="https://github.com/x/y.git",
            working_dir="/work/rm-inherit", review_model="sonnet",
        )
        task = await db.create_task(
            id="rm-inherit/task1", project_id="rm-inherit", goal="Test",
        )
        resolved = await db.resolve_config(task["id"])
        assert resolved["review_model"] == "sonnet"

    async def test_project_auto_pr_falls_through_to_task(self, db):
        """project.auto_pr=True is stored as resolved value when dispatch_task has no explicit auto_pr."""
        await db.create_project(
            id="autopr-proj", repo="https://github.com/x/y.git",
            working_dir="/work/autopr-proj",
        )
        await db.update_project("autopr-proj", auto_pr=True)

        # Use dispatch_task with concurrency full so it creates-but-queues the task.
        # This exercises the config resolution path before create_task.
        import ouvrage.db as _db
        for i in range(_db.DEFAULT_MAX_CONCURRENT):
            t = await db.create_task(
                id=f"autopr-proj/filler-{i}", project_id="autopr-proj", goal=f"Filler {i}",
            )
            await db.update_task(t["id"], status="working")

        from ouvrage.dispatch.engine import dispatch_task
        with patch("ouvrage.dispatch.engine.notify", AsyncMock()):
            result = await dispatch_task(
                project_id="autopr-proj",
                task_id="autopr-proj/task1",
                goal="Test project auto_pr inheritance",
            )

        assert result["queued"] is True
        task = await db.get_task("autopr-proj/task1")
        # auto_pr should have been resolved from project (True) since none was passed
        assert task["auto_pr"] == 1 or task["auto_pr"] is True

    async def test_task_review_model_overrides_project(self, db):
        """task.review_model wins over project.review_model."""
        await db.create_project(
            id="rm-override", repo="https://github.com/x/y.git",
            working_dir="/work/rm-override", review_model="sonnet",
        )
        task = await db.create_task(
            id="rm-override/task1", project_id="rm-override", goal="Test",
            review_model="opus",
        )
        resolved = await db.resolve_config(task["id"])
        assert resolved["review_model"] == "opus"


# ---------------------------------------------------------------------------
# update_task: new settable fields
# ---------------------------------------------------------------------------

class TestUpdateTaskNewFields:
    async def test_update_task_max_turns(self, db, sample_project):
        task = await db.create_task(
            id="test-project/turns-task", project_id="test-project", goal="Test",
        )
        updated = await db.update_task(task["id"], max_turns=50)
        assert updated["max_turns"] == 50

    async def test_update_task_max_wall_clock(self, db, sample_project):
        task = await db.create_task(
            id="test-project/wc-task", project_id="test-project", goal="Test",
        )
        updated = await db.update_task(task["id"], max_wall_clock=120)
        assert updated["max_wall_clock"] == 120

    async def test_update_task_review_model(self, db, sample_project):
        task = await db.create_task(
            id="test-project/rm-task", project_id="test-project", goal="Test",
        )
        updated = await db.update_task(task["id"], review_model="sonnet")
        assert updated["review_model"] == "sonnet"


# ---------------------------------------------------------------------------
# max_test_retries / max_review_retries gate wiring
# ---------------------------------------------------------------------------

class TestGateRetryFields:
    async def test_max_test_retries_column_exists(self, db, sample_project):
        """max_test_retries column exists on tasks table."""
        async with db.get_db() as conn:
            cols = await conn.execute_fetchall("PRAGMA table_info(tasks)")
            col_names = [c["name"] for c in cols]
            assert "max_test_retries" in col_names

    async def test_max_review_retries_column_exists(self, db, sample_project):
        """max_review_retries column exists on tasks table."""
        async with db.get_db() as conn:
            cols = await conn.execute_fetchall("PRAGMA table_info(tasks)")
            col_names = [c["name"] for c in cols]
            assert "max_review_retries" in col_names

    async def test_max_test_retries_updatable(self, db, sample_project):
        task = await db.create_task(
            id="test-project/upd-retry", project_id="test-project", goal="Test",
        )
        updated = await db.update_task(task["id"], max_test_retries=2)
        assert updated["max_test_retries"] == 2

    async def test_max_review_retries_updatable(self, db, sample_project):
        task = await db.create_task(
            id="test-project/upd-rev-retry", project_id="test-project", goal="Test",
        )
        updated = await db.update_task(task["id"], max_review_retries=3)
        assert updated["max_review_retries"] == 3

    async def test_max_test_retries_settable_at_creation(self, db, sample_project):
        """max_test_retries can be set at task creation time (e.g. from dispatch)."""
        task = await db.create_task(
            id="test-project/create-retry", project_id="test-project", goal="Test",
            max_test_retries=5,
        )
        assert task["max_test_retries"] == 5
        fetched = await db.get_task(task["id"])
        assert fetched["max_test_retries"] == 5

    async def test_max_review_retries_settable_at_creation(self, db, sample_project):
        """max_review_retries can be set at task creation time."""
        task = await db.create_task(
            id="test-project/create-rev-retry", project_id="test-project", goal="Test",
            max_review_retries=1,
        )
        assert task["max_review_retries"] == 1


# ---------------------------------------------------------------------------
# dispatch_task: boolean schema fix — auto_release_worktree resolves via engine
# ---------------------------------------------------------------------------

class TestAutoReleaseWorktreeResolution:
    @pytest.fixture(autouse=True)
    def mock_git(self):
        with patch("ouvrage.dispatch.engine._run_as_worker") as mock_worker, \
             patch("ouvrage.dispatch.engine.setup_worktree") as mock_setup, \
             patch("ouvrage.dispatch.engine.cleanup_worktree") as mock_cleanup:
            mock_setup.return_value = "/tmp/fake-worktree"
            mock_worker.return_value = None
            mock_cleanup.return_value = None
            yield

    async def test_auto_release_worktree_defaults_true_via_resolution(self, db, sample_project):
        """When auto_release_worktree not passed, resolves to system default True."""
        from ouvrage.dispatch.engine import dispatch_task
        await dispatch_task(
            project_id="test-project", task_id="test-project/arw-default",
            goal="Test", held=True,
        )
        task = await db.get_task("test-project/arw-default")
        assert task["auto_release_worktree"] == 1 or task["auto_release_worktree"] is True

    async def test_auto_release_worktree_explicit_false(self, db, sample_project):
        """Explicit False is respected and not overridden."""
        from ouvrage.dispatch.engine import dispatch_task
        await dispatch_task(
            project_id="test-project", task_id="test-project/arw-false",
            goal="Test", held=True, auto_release_worktree=False,
        )
        task = await db.get_task("test-project/arw-false")
        assert task["auto_release_worktree"] == 0 or task["auto_release_worktree"] is False


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

    async def test_review_ignore_patterns_none_by_default(self, db, sample_project):
        """review_ignore_patterns is absent when not set at creation."""
        comp = await db.create_component(
            id="no-ignore-comp", project_id="test-project", name="Test",
        )
        fetched = await db.get_component("no-ignore-comp")
        assert not fetched.get("review_ignore_patterns")
