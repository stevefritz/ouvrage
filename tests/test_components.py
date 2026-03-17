"""Tests for v5 components: CRUD, config inheritance, conversations, dispatch integration."""

import json
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

    async def test_create_component_with_config(self, db, sample_project):
        comp = await db.create_component(
            id="full-config", project_id="test-project", name="Full Config",
            description="Everything set",
            phase="building",
            base_branch="feature/full",
            model="opus",
            auto_test=True,
            auto_review=False,
            review_model="sonnet",
            max_test_retries=5,
            max_review_retries=1,
            auto_pr=True,
            auto_merge=False,
            max_turns=100,
            max_wall_clock=30,
            env_overrides={"DB": "sqlite"},
            secrets={"API_KEY": "secret123"},
        )
        assert comp["model"] == "opus"
        assert comp["base_branch"] == "feature/full"
        assert comp["env_overrides"] == {"DB": "sqlite"}
        assert comp["secrets"] == {"API_KEY": "secret123"}
        assert comp["max_turns"] == 100

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

    async def test_update_component(self, db, sample_project):
        await db.create_component(
            id="updatable", project_id="test-project", name="Before",
        )
        updated = await db.update_component("updatable", name="After", phase="building", model="opus")
        assert updated["name"] == "After"
        assert updated["phase"] == "building"
        assert updated["model"] == "opus"

    async def test_update_component_env_overrides(self, db, sample_project):
        await db.create_component(
            id="env-test", project_id="test-project", name="Env",
        )
        updated = await db.update_component("env-test", env_overrides={"FOO": "bar"})
        assert updated["env_overrides"] == {"FOO": "bar"}

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
        assert comps[0]["task_count"] == 2

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
        assert "widget-redesign" in comp["conversations"]


# ---------------------------------------------------------------------------
# Config Inheritance
# ---------------------------------------------------------------------------

class TestConfigInheritance:
    async def test_task_overrides_component_overrides_project(self, db, sample_project):
        """Task value > component value > project value."""
        await db.create_component(
            id="mid-layer", project_id="test-project", name="Mid",
            model="sonnet", max_turns=80,
        )
        task = await db.create_task(
            id="test-project/override", project_id="test-project",
            goal="Override test", component_id="mid-layer",
            model="opus",  # task overrides component's "sonnet"
        )
        resolved = await db.resolve_config(task["id"])
        assert resolved["model"] == "opus"  # task wins
        assert resolved["max_turns"] == 80  # component wins over project (150)

    async def test_null_task_falls_through_to_component(self, db, sample_project):
        """When task has no value, component's value is used."""
        await db.create_component(
            id="fallthrough", project_id="test-project", name="Fallthrough",
            model="sonnet", max_turns=77,
        )
        task = await db.create_task(
            id="test-project/fallthrough", project_id="test-project",
            goal="Fallthrough", component_id="fallthrough",
        )
        resolved = await db.resolve_config(task["id"])
        assert resolved["model"] == "sonnet"  # from component
        assert resolved["max_turns"] == 77  # from component (project has 150)

    async def test_null_component_falls_through_to_project(self, db, sample_project):
        """When component has no value, project's value is used."""
        # sample_project has model="opus", max_turns=150
        await db.create_component(
            id="empty-comp", project_id="test-project", name="Empty",
        )
        task = await db.create_task(
            id="test-project/empty-comp-task", project_id="test-project",
            goal="Empty comp", component_id="empty-comp",
        )
        resolved = await db.resolve_config(task["id"])
        assert resolved["model"] == "opus"  # from project
        assert resolved["max_turns"] == 150  # from project
        assert resolved["test_command"] == "python -m pytest tests/ -v"  # from project

    async def test_falls_through_to_system_defaults(self, db, sample_project):
        """When nobody sets a value, system defaults are used."""
        await db.create_component(
            id="defaults-comp", project_id="test-project", name="Defaults",
        )
        task = await db.create_task(
            id="test-project/defaults-task", project_id="test-project",
            goal="Defaults", component_id="defaults-comp",
        )
        resolved = await db.resolve_config(task["id"])
        assert resolved["auto_test"] is True  # system default
        assert resolved["auto_review"] is True  # system default
        assert resolved["review_model"] == "opus"  # system default
        assert resolved["max_test_retries"] == 3  # system default
        assert resolved["max_review_retries"] == 2  # system default
        assert resolved["auto_pr"] is False  # system default
        assert resolved["auto_merge"] is False  # system default

    async def test_env_overrides_shallow_merge(self, db, sample_project):
        """env_overrides: project base ← component ← task (per-key wins)."""
        # sample_project has env_overrides={"NODE_ENV": "test", "DEBUG": "1"}
        await db.create_component(
            id="env-merge", project_id="test-project", name="Env Merge",
            env_overrides={"NODE_ENV": "development", "EXTRA": "from-component"},
        )
        task = await db.create_task(
            id="test-project/env-merge-task", project_id="test-project",
            goal="Env merge", component_id="env-merge",
        )
        resolved = await db.resolve_config(task["id"])
        env = resolved["env_overrides"]
        # Component overrides project's NODE_ENV
        assert env["NODE_ENV"] == "development"
        # Project's DEBUG preserved
        assert env["DEBUG"] == "1"
        # Component adds EXTRA
        assert env["EXTRA"] == "from-component"

    async def test_secrets_shallow_merge(self, db, sample_project):
        """secrets: same shallow merge behavior as env_overrides."""
        # Project has no secrets by default, so set one
        await db.update_project("test-project", env_overrides={"NODE_ENV": "test"})

        await db.create_component(
            id="secret-merge", project_id="test-project", name="Secret Merge",
            secrets={"API_KEY": "component-key", "DB_PASS": "comp-pass"},
        )
        task = await db.create_task(
            id="test-project/secret-merge-task", project_id="test-project",
            goal="Secret merge", component_id="secret-merge",
        )
        resolved = await db.resolve_config(task["id"])
        assert resolved["secrets"]["API_KEY"] == "component-key"
        assert resolved["secrets"]["DB_PASS"] == "comp-pass"

    async def test_task_without_component_backward_compat(self, db, sample_project):
        """Tasks without component_id work exactly as before."""
        task = await db.create_task(
            id="test-project/no-comp", project_id="test-project",
            goal="No component",
        )
        resolved = await db.resolve_config(task["id"])
        # Should fall through to project and system defaults
        assert resolved["model"] == "opus"  # from project
        assert resolved["max_turns"] == 150  # from project
        assert resolved["auto_test"] is True  # system default
        assert resolved["env_overrides"] == {"NODE_ENV": "test", "DEBUG": "1"}  # from project

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
