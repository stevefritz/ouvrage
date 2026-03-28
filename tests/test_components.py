"""Tests for v5 components: CRUD, config inheritance, conversations, dispatch integration."""

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
