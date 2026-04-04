"""Schema validation and CRUD tests for the switchboard database.

Tests run against in-memory SQLite via init_db(). Never touches the
production database.
"""

import json

import pytest


# ===========================================================================
# Schema validation
# ===========================================================================

class TestSchemaValidation:
    """Verify init_db() creates all expected tables and columns."""

    async def _get_tables(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            return {r["name"] for r in rows}

    async def test_all_expected_tables_exist(self, db):
        tables = await self._get_tables(db)
        expected = {
            "conversations", "projects", "tasks", "messages",
            "task_checklist", "task_artifacts", "task_tags", "subtasks",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    async def test_tasks_has_gate_pipeline_columns(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("PRAGMA table_info(tasks)")
            col_names = {r["name"] for r in rows}

        gate_cols = {
            "auto_test", "auto_review", "gate_status", "gate_retries",
            "max_gate_retries", "gate_passed_at", "depends_on",
            "parent_task_id", "auto_pr", "review_model",
        }
        assert gate_cols.issubset(col_names), f"Missing gate columns: {gate_cols - col_names}"

    async def test_tasks_has_core_columns(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("PRAGMA table_info(tasks)")
            col_names = {r["name"] for r in rows}

        core_cols = {
            "id", "project_id", "goal", "status", "phase", "branch",
            "worktree_path", "session_id", "max_turns", "max_wall_clock",
            "total_input_tokens", "total_output_tokens", "total_cost_usd",
            "dispatch_count", "last_activity", "created_at", "updated_at",
            "jira_ticket", "conversation_id", "model",
        }
        assert core_cols.issubset(col_names), f"Missing core columns: {core_cols - col_names}"

    async def test_foreign_keys_enabled(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("PRAGMA foreign_keys")
            assert rows[0][0] == 1

    async def test_wal_mode_enabled(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("PRAGMA journal_mode")
            assert rows[0][0] == "wal"

    async def test_messages_has_task_id_column(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("PRAGMA table_info(messages)")
            col_names = {r["name"] for r in rows}
        assert "task_id" in col_names
        assert "conversation_id" in col_names

    async def test_projects_has_model_column(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("PRAGMA table_info(projects)")
            col_names = {r["name"] for r in rows}
        assert "model" in col_names


# ===========================================================================
# Project CRUD
# ===========================================================================

class TestProjectCRUD:

    async def test_create_and_get_project(self, db):
        proj = await db.create_project(
            id="crud-proj", repo="git@github.com:test/repo.git",
            working_dir="/work/test",
        )
        assert proj["id"] == "crud-proj"
        assert proj["default_branch"] == "main"

        fetched = await db.get_project("crud-proj")
        assert fetched is not None
        assert fetched["repo"] == "git@github.com:test/repo.git"

    async def test_project_env_overrides_json_roundtrip(self, db):
        env = {"API_KEY": "test-key", "DEBUG": "true", "nested": "value"}
        await db.create_project(
            id="env-proj", repo="git@x.git", working_dir="/w",
            env_overrides=env,
        )
        fetched = await db.get_project("env-proj")
        assert fetched["env_overrides"] == env

    async def test_update_project(self, db):
        await db.create_project(id="upd-proj", repo="git@x.git", working_dir="/w")
        updated = await db.update_project("upd-proj", test_command="make test")
        assert updated["test_command"] == "make test"

    async def test_update_project_display_name(self, db):
        await db.create_project(id="name-proj", repo="git@x.git", working_dir="/w")
        updated = await db.update_project("name-proj", display_name="My Project")
        assert updated["display_name"] == "My Project"
        fetched = await db.get_project("name-proj")
        assert fetched["display_name"] == "My Project"

    async def test_list_projects(self, db):
        await db.create_project(id="list-a", repo="git@a.git", working_dir="/a")
        await db.create_project(id="list-b", repo="git@b.git", working_dir="/b")
        projects = await db.list_projects()
        ids = {p["id"] for p in projects}
        assert {"list-a", "list-b"}.issubset(ids)

    async def test_get_nonexistent_project(self, db):
        result = await db.get_project("nope")
        assert result is None


# ===========================================================================
# Task CRUD
# ===========================================================================

class TestTaskCRUD:

    async def _seed_project(self, db):
        await db.create_project(id="task-proj", repo="git@x.git", working_dir="/w")

    async def test_create_and_get_task(self, db):
        await self._seed_project(db)
        task = await db.create_task(
            id="task-proj/feat-1", project_id="task-proj", goal="Build feature 1",
        )
        assert task["status"] == "ready"
        assert task["branch"] == "feat-1"  # short name extracted from id

        fetched = await db.get_task("task-proj/feat-1")
        assert fetched is not None
        assert fetched["goal"] == "Build feature 1"

    async def test_task_gate_fields_on_create(self, db):
        await self._seed_project(db)
        task = await db.create_task(
            id="task-proj/gate-test", project_id="task-proj", goal="Gate test",
            auto_test=True, auto_review=True, review_model="sonnet",
            depends_on="task-proj/other", auto_pr=True,
        )
        assert task["auto_test"] is True
        assert task["auto_review"] is True
        assert task["review_model"] == "sonnet"
        assert task["depends_on"] == "task-proj/other"
        assert task["auto_pr"] is True

    async def test_update_task_status(self, db):
        await self._seed_project(db)
        await db.create_task(id="task-proj/status", project_id="task-proj", goal="Status test")
        updated = await db.update_task("task-proj/status", status="working")
        assert updated["status"] == "working"

        updated = await db.update_task("task-proj/status", status="completed")
        assert updated["status"] == "completed"

    async def test_update_task_gate_fields(self, db):
        await self._seed_project(db)
        await db.create_task(id="task-proj/gate", project_id="task-proj", goal="Gate")
        ts = db.now_iso()
        updated = await db.update_task("task-proj/gate",
            gate_status="passed", gate_passed_at=ts, gate_retries=2,
        )
        assert updated["gate_status"] == "passed"
        assert updated["gate_passed_at"] == ts
        assert updated["gate_retries"] == 2

    async def test_list_tasks_by_project(self, db):
        await self._seed_project(db)
        await db.create_task(id="task-proj/t1", project_id="task-proj", goal="T1")
        await db.create_task(id="task-proj/t2", project_id="task-proj", goal="T2")
        tasks = await db.list_tasks(project_id="task-proj")
        assert len(tasks) == 2

    async def test_get_nonexistent_task(self, db):
        result = await db.get_task("nope")
        assert result is None


# ===========================================================================
# Message CRUD
# ===========================================================================

class TestMessageCRUD:

    async def _seed(self, db):
        await db.create_project(id="msg-proj", repo="git@x.git", working_dir="/w")
        await db.create_task(id="msg-proj/t1", project_id="msg-proj", goal="Msg test")

    async def test_post_and_read_task_message(self, db):
        await self._seed(db)
        await db.post_task_message(
            task_id="msg-proj/t1", author="cc-worker",
            content="Starting work", type="progress",
        )
        thread = await db.read_task_messages("msg-proj/t1")
        assert len(thread["messages"]) == 1
        assert thread["messages"][0]["author"] == "cc-worker"

    async def test_cursor_pagination(self, db):
        await self._seed(db)
        await db.post_task_message(task_id="msg-proj/t1", author="a", content="msg 1")
        await db.post_task_message(task_id="msg-proj/t1", author="a", content="msg 2")
        await db.post_task_message(task_id="msg-proj/t1", author="a", content="msg 3")

        # Read all, get cursor
        result = await db.read_task_messages("msg-proj/t1")
        assert len(result["messages"]) == 3
        cursor = result["cursor"]

        # Add one more and read with cursor
        await db.post_task_message(task_id="msg-proj/t1", author="a", content="msg 4")
        result2 = await db.read_task_messages("msg-proj/t1", after=cursor)
        assert len(result2["messages"]) == 1
        assert result2["messages"][0]["content"] == "msg 4"

    async def test_pinned_message(self, db):
        await db.create_conversation(id="pin-conv", project="test", goal="Pin test")
        await db.post_message(
            conversation_id="pin-conv", author="stephen",
            content="This is the spec", type="spec", pinned=True,
        )
        pinned = await db.get_pinned("pin-conv")
        assert pinned is not None
        assert pinned["content"] == "This is the spec"

    async def test_conversation_messages(self, db):
        await db.create_conversation(id="conv-1", project="test", goal="Test")
        await db.post_message(conversation_id="conv-1", author="a", content="Hello")
        await db.post_message(conversation_id="conv-1", author="b", content="World")
        result = await db.read_messages("conv-1")
        assert len(result["messages"]) == 2


# ===========================================================================
# Conversation listing aggregates (has_pinned, pinned_title, message_count)
# ===========================================================================

class TestConversationListAggregates:
    """Tests for _list_with_aggregates via list_conversations().

    Covers has_pinned / pinned_title fields and correct message_count
    when pinned messages exist (guards against JOIN inflation).
    """

    async def test_has_pinned_false_when_no_pinned_message(self, db):
        await db.create_conversation(id="agg-conv-1", project="agg-proj", goal="No pin")
        await db.post_message(conversation_id="agg-conv-1", author="u", content="hello")
        convs = await db.list_conversations(project="agg-proj")
        assert len(convs) == 1
        assert convs[0]["has_pinned"] == 0  # SQLite returns 0/1 for bool expressions

    async def test_has_pinned_true_when_pinned_message_exists(self, db):
        await db.create_conversation(id="agg-conv-2", project="agg-proj2", goal="Has pin")
        await db.post_message(
            conversation_id="agg-conv-2", author="u", content="spec",
            title="Spec title", pinned=True,
        )
        convs = await db.list_conversations(project="agg-proj2")
        assert len(convs) == 1
        assert convs[0]["has_pinned"]

    async def test_pinned_title_returned_correctly(self, db):
        await db.create_conversation(id="agg-conv-3", project="agg-proj3", goal="Pin title test")
        await db.post_message(
            conversation_id="agg-conv-3", author="u", content="body",
            title="My pinned title", pinned=True,
        )
        convs = await db.list_conversations(project="agg-proj3")
        assert convs[0]["pinned_title"] == "My pinned title"

    async def test_pinned_title_null_when_no_pinned(self, db):
        await db.create_conversation(id="agg-conv-4", project="agg-proj4", goal="No pin title")
        await db.post_message(conversation_id="agg-conv-4", author="u", content="hello")
        convs = await db.list_conversations(project="agg-proj4")
        assert convs[0]["pinned_title"] is None

    async def test_message_count_correct_with_pinned_message(self, db):
        """Pinned JOIN must not inflate message_count."""
        await db.create_conversation(id="agg-conv-5", project="agg-proj5", goal="Count test")
        await db.post_message(conversation_id="agg-conv-5", author="u", content="msg1")
        await db.post_message(conversation_id="agg-conv-5", author="u", content="msg2",
                              title="Pinned one", pinned=True)
        await db.post_message(conversation_id="agg-conv-5", author="u", content="msg3")
        convs = await db.list_conversations(project="agg-proj5")
        assert convs[0]["message_count"] == 3

    async def test_message_count_with_multiple_pinned_messages(self, db):
        """Multiple pinned messages must not multiply message_count."""
        await db.create_conversation(id="agg-conv-6", project="agg-proj6", goal="Multi-pin test")
        await db.post_message(conversation_id="agg-conv-6", author="u", content="msg1",
                              title="Pin A", pinned=True)
        await db.post_message(conversation_id="agg-conv-6", author="u", content="msg2",
                              title="Pin B", pinned=True)
        await db.post_message(conversation_id="agg-conv-6", author="u", content="msg3")
        convs = await db.list_conversations(project="agg-proj6")
        assert convs[0]["message_count"] == 3

    async def test_pinned_title_returns_most_recent_when_multiple_pinned(self, db):
        """When multiple pinned messages exist, pinned_title is deterministic (most recent)."""
        await db.create_conversation(id="agg-conv-7", project="agg-proj7", goal="Multi-pin title")
        await db.post_message(conversation_id="agg-conv-7", author="u", content="old",
                              title="Older pin", pinned=True)
        await db.post_message(conversation_id="agg-conv-7", author="u", content="new",
                              title="Newer pin", pinned=True)
        convs = await db.list_conversations(project="agg-proj7")
        assert convs[0]["pinned_title"] == "Newer pin"


# ===========================================================================
# Checklist operations
# ===========================================================================

class TestChecklistOperations:

    async def _seed(self, db):
        await db.create_project(id="cl-proj", repo="git@x.git", working_dir="/w")
        await db.create_task(id="cl-proj/t1", project_id="cl-proj", goal="CL test")

    async def test_create_and_get_checklist(self, db):
        await self._seed(db)
        items = await db.create_checklist_items("cl-proj/t1", ["Item 1", "Item 2", "Item 3"])
        assert len(items) == 3
        assert all(not i["done"] for i in items)

        fetched = await db.get_checklist("cl-proj/t1")
        assert len(fetched) == 3

    async def test_mark_item_done(self, db):
        await self._seed(db)
        items = await db.create_checklist_items("cl-proj/t1", ["Do the thing"])
        item_id = items[0]["id"]

        updated = await db.update_checklist_item(item_id, done=True)
        assert updated["done"] is True

    async def test_add_item(self, db):
        await self._seed(db)
        await db.create_checklist_items("cl-proj/t1", ["Original"])
        new_item = await db.add_checklist_item("cl-proj/t1", "Added later")
        assert new_item["item"] == "Added later"

        all_items = await db.get_checklist("cl-proj/t1")
        assert len(all_items) == 2

    async def test_remove_item(self, db):
        await self._seed(db)
        items = await db.create_checklist_items("cl-proj/t1", ["Keep", "Remove"])
        removed = await db.remove_checklist_item(items[1]["id"])
        assert removed["removed"] is True

        remaining = await db.get_checklist("cl-proj/t1")
        assert len(remaining) == 1
        assert remaining[0]["item"] == "Keep"

    async def test_update_item_text(self, db):
        await self._seed(db)
        items = await db.create_checklist_items("cl-proj/t1", ["Old text"])
        updated = await db.update_checklist_item_text(items[0]["id"], "New text")
        assert updated["item"] == "New text"
