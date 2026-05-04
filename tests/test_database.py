"""Schema validation and CRUD tests for the ouvrage database.

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
            # :memory: DBs return "memory"; file-based DBs return "wal".
            assert rows[0][0] in ("wal", "memory")

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


# ===========================================================================
# list_merged_tasks_since
# ===========================================================================

class TestListMergedTasksSince:

    async def _seed(self, db):
        await db.create_project(id="merge-proj", repo="git@x.git", working_dir="/w")

    async def test_returns_all_merged_when_since_is_none(self, db):
        await self._seed(db)
        await db.create_task(id="merge-proj/t1", project_id="merge-proj", goal="Task 1")
        await db.create_task(id="merge-proj/t2", project_id="merge-proj", goal="Task 2")
        await db.update_task("merge-proj/t1", pr_status="merged", merged_at="2026-01-01T10:00:00Z")
        await db.update_task("merge-proj/t2", pr_status="merged", merged_at="2026-01-02T10:00:00Z")

        results = await db.list_merged_tasks_since("merge-proj", None)

        assert len(results) == 2
        assert results[0]["id"] == "merge-proj/t1"
        assert results[1]["id"] == "merge-proj/t2"
        # Ascending merged_at order
        assert results[0]["merged_at"] < results[1]["merged_at"]

    async def test_filters_by_since_iso(self, db):
        await self._seed(db)
        await db.create_task(id="merge-proj/old", project_id="merge-proj", goal="Old task")
        await db.create_task(id="merge-proj/new", project_id="merge-proj", goal="New task")
        await db.update_task("merge-proj/old", pr_status="merged", merged_at="2026-01-01T00:00:00Z")
        await db.update_task("merge-proj/new", pr_status="merged", merged_at="2026-01-15T00:00:00Z")

        results = await db.list_merged_tasks_since("merge-proj", "2026-01-10T00:00:00Z")

        assert len(results) == 1
        assert results[0]["id"] == "merge-proj/new"

    async def test_returns_empty_for_project_with_no_merges(self, db):
        await self._seed(db)
        await db.create_task(id="merge-proj/open", project_id="merge-proj", goal="Open task")
        await db.update_task("merge-proj/open", pr_status="open")

        results = await db.list_merged_tasks_since("merge-proj", None)

        assert results == []


# ===========================================================================
# Living Docs schema v2
# ===========================================================================

class TestLivingDocsSchema:
    """Verify schema v2 tables, columns, constraints, and FK cascade."""

    async def _get_col_names(self, db, table):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(f"PRAGMA table_info({table})")
            return {r["name"] for r in rows}

    async def _get_tables(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            return {r["name"] for r in rows}

    # --- Schema presence ---

    async def test_new_tables_exist(self, db):
        tables = await self._get_tables(db)
        expected = {"reference_doc_configs", "reference_doc_runs", "files_embeddings", "file_chunks"}
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    async def test_files_has_role_column(self, db):
        col_names = await self._get_col_names(db, "files")
        assert "role" in col_names

    async def test_projects_has_living_docs_columns(self, db):
        col_names = await self._get_col_names(db, "projects")
        assert "living_docs_enabled" in col_names
        assert "reference_doc_path" in col_names
        assert "living_docs_regen_interval_hours" in col_names

    # --- Idempotency ---

    async def test_init_db_idempotent(self, db):
        import ouvrage.db as _db
        await _db.init_db()  # second run must not raise
        tables = await self._get_tables(db)
        assert "reference_doc_configs" in tables
        assert "reference_doc_runs" in tables

    # --- UNIQUE constraint: reference_doc_configs(project_id, slug) ---

    async def test_reference_doc_configs_unique_slug(self, db):
        import aiosqlite
        await db.create_project(id="ld-proj", repo="git@x.git", working_dir="/w")
        async with db.get_db() as conn:
            await conn.execute(
                """INSERT INTO reference_doc_configs (id, project_id, slug, title, brief)
                   VALUES ('cfg-1', 'ld-proj', 'api-overview', 'API Overview', 'Describes the API')"""
            )
            await conn.commit()
            with pytest.raises(Exception):
                await conn.execute(
                    """INSERT INTO reference_doc_configs (id, project_id, slug, title, brief)
                       VALUES ('cfg-2', 'ld-proj', 'api-overview', 'Duplicate Slug', 'Should fail')"""
                )

    # --- CHECK constraint: reference_doc_runs.outcome ---

    async def test_reference_doc_runs_outcome_check(self, db):
        await db.create_project(id="run-proj", repo="git@x.git", working_dir="/w")
        task = await db.create_task(id="run-proj/t1", project_id="run-proj", goal="Gen docs")
        async with db.get_db() as conn:
            with pytest.raises(Exception):
                await conn.execute(
                    """INSERT INTO reference_doc_runs (project_id, task_id, outcome)
                       VALUES ('run-proj', 'run-proj/t1', 'invalid_outcome')"""
                )

    # --- FK cascade: projects → reference_doc_configs ---

    async def test_reference_doc_configs_cascade_delete(self, db):
        await db.create_project(id="cascade-proj", repo="git@x.git", working_dir="/w")
        async with db.get_db() as conn:
            await conn.execute(
                """INSERT INTO reference_doc_configs (id, project_id, slug, title, brief)
                   VALUES ('cfg-cascade', 'cascade-proj', 'some-doc', 'Some Doc', 'Brief')"""
            )
            await conn.commit()
            rows = await conn.execute_fetchall(
                "SELECT id FROM reference_doc_configs WHERE project_id = 'cascade-proj'"
            )
            assert len(rows) == 1

        await db.delete_project("cascade-proj")

        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT id FROM reference_doc_configs WHERE project_id = 'cascade-proj'"
            )
            assert rows == [], "FK cascade from projects should delete reference_doc_configs rows"

    # --- FK cascade: files → files_embeddings ---

    async def test_files_embeddings_cascade_delete(self, db):
        await db.create_project(id="fe-proj", repo="git@x.git", working_dir="/w")
        async with db.get_db() as conn:
            await conn.execute(
                """INSERT INTO files (id, filename, stored_path, project_id)
                   VALUES ('file-fe-1', 'doc.md', '/tmp/doc.md', 'fe-proj')"""
            )
            await conn.execute(
                "INSERT INTO files_embeddings (file_id) VALUES ('file-fe-1')"
            )
            await conn.commit()
            rows = await conn.execute_fetchall(
                "SELECT file_id FROM files_embeddings WHERE file_id = 'file-fe-1'"
            )
            assert len(rows) == 1

            await conn.execute("DELETE FROM files WHERE id = 'file-fe-1'")
            await conn.commit()

            rows = await conn.execute_fetchall(
                "SELECT file_id FROM files_embeddings WHERE file_id = 'file-fe-1'"
            )
            assert rows == [], "FK cascade from files should delete files_embeddings rows"

    # --- FK cascade: files → file_chunks ---

    async def test_file_chunks_cascade_delete(self, db):
        await db.create_project(id="fc-proj", repo="git@x.git", working_dir="/w")
        async with db.get_db() as conn:
            await conn.execute(
                """INSERT INTO files (id, filename, stored_path, project_id)
                   VALUES ('file-fc-1', 'doc.md', '/tmp/doc.md', 'fc-proj')"""
            )
            await conn.execute(
                """INSERT INTO file_chunks (file_id, chunk_index, content)
                   VALUES ('file-fc-1', 0, 'Hello world')"""
            )
            await conn.commit()
            rows = await conn.execute_fetchall(
                "SELECT id FROM file_chunks WHERE file_id = 'file-fc-1'"
            )
            assert len(rows) == 1

            await conn.execute("DELETE FROM files WHERE id = 'file-fc-1'")
            await conn.commit()

            rows = await conn.execute_fetchall(
                "SELECT id FROM file_chunks WHERE file_id = 'file-fc-1'"
            )
            assert rows == [], "FK cascade from files should delete file_chunks rows"

    # --- delete_file role guard ---

    async def test_delete_file_raises_for_reference_doc(self, db):
        await db.create_project(id="guard-proj", repo="git@x.git", working_dir="/w")
        async with db.get_db() as conn:
            await conn.execute(
                """INSERT INTO files (id, filename, stored_path, project_id, role)
                   VALUES ('ref-file-1', 'ref.md', '/tmp/ref.md', 'guard-proj', 'reference_doc')"""
            )
            await conn.commit()

        with pytest.raises(ValueError, match="reference doc and cannot be deleted directly"):
            await db.delete_file("ref-file-1")

        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("SELECT id FROM files WHERE id = 'ref-file-1'")
            assert len(rows) == 1, "Row must still exist after blocked delete"

    async def test_delete_file_works_for_upload_role(self, db):
        await db.create_project(id="upload-proj", repo="git@x.git", working_dir="/w")
        async with db.get_db() as conn:
            await conn.execute(
                """INSERT INTO files (id, filename, stored_path, project_id)
                   VALUES ('upload-file-1', 'up.txt', '/tmp/up.txt', 'upload-proj')"""
            )
            await conn.commit()

        result = await db.delete_file("upload-file-1")
        assert result is True

        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("SELECT id FROM files WHERE id = 'upload-file-1'")
            assert rows == []

    async def test_delete_reference_doc_files_bypasses_guard_and_cascades(self, db):
        await db.create_project(id="bypass-proj", repo="git@x.git", working_dir="/w")
        async with db.get_db() as conn:
            await conn.execute(
                """INSERT INTO files (id, filename, stored_path, project_id, role)
                   VALUES ('bypass-file-1', 'ref.md', '/tmp/ref.md', 'bypass-proj', 'reference_doc')"""
            )
            await conn.execute(
                "INSERT INTO files_embeddings (file_id) VALUES ('bypass-file-1')"
            )
            await conn.execute(
                """INSERT INTO file_chunks (file_id, chunk_index, content)
                   VALUES ('bypass-file-1', 0, 'chunk content')"""
            )
            await conn.commit()

        result = await db.delete_reference_doc_files("bypass-file-1")
        assert result is True

        async with db.get_db() as conn:
            files_rows = await conn.execute_fetchall(
                "SELECT id FROM files WHERE id = 'bypass-file-1'"
            )
            emb_rows = await conn.execute_fetchall(
                "SELECT file_id FROM files_embeddings WHERE file_id = 'bypass-file-1'"
            )
            chunk_rows = await conn.execute_fetchall(
                "SELECT id FROM file_chunks WHERE file_id = 'bypass-file-1'"
            )
        assert files_rows == [], "files row must be gone"
        assert emb_rows == [], "files_embeddings must cascade-delete"
        assert chunk_rows == [], "file_chunks must cascade-delete"


# ===========================================================================
# Reference doc helpers
# ===========================================================================

class TestReferenceDocHelpers:
    """Tests for ouvrage/db/reference_docs.py helpers."""

    # shared project/task IDs — each test method creates its own to avoid collisions
    async def _setup_project(self, db, proj_id="rd-proj"):
        await db.create_project(id=proj_id, repo="git@x.git", working_dir="/w")
        return proj_id

    async def _setup_task(self, db, proj_id="rd-proj", task_id=None):
        tid = task_id or f"{proj_id}/t1"
        await db.create_task(id=tid, project_id=proj_id, goal="Regen docs")
        return tid

    # --- upsert_config happy path ---

    async def test_upsert_config_creates_row(self, db):
        await self._setup_project(db, "uc-proj")
        row = await db.upsert_config(
            project_id="uc-proj", slug="api-ref", title="API Reference", brief="All endpoints"
        )
        assert row["project_id"] == "uc-proj"
        assert row["slug"] == "api-ref"
        assert row["title"] == "API Reference"
        assert row["brief"] == "All endpoints"
        assert row["id"] is not None

    async def test_upsert_config_idempotent(self, db):
        await self._setup_project(db, "ui-proj")
        row1 = await db.upsert_config(
            project_id="ui-proj", slug="overview", title="Overview v1", brief="Brief v1"
        )
        row2 = await db.upsert_config(
            project_id="ui-proj", slug="overview", title="Overview v2", brief="Brief v2"
        )
        # Same row (same id), updated title/brief
        assert row1["id"] == row2["id"]
        assert row2["title"] == "Overview v2"
        assert row2["brief"] == "Brief v2"

    async def test_upsert_config_source_hints(self, db):
        await self._setup_project(db, "sh-proj")
        row = await db.upsert_config(
            project_id="sh-proj", slug="hints-doc", title="T", brief="B",
            source_hints="src/*.py"
        )
        assert row["source_hints"] == "src/*.py"

    # --- get_config ---

    async def test_get_config_returns_row(self, db):
        await self._setup_project(db, "gc-proj")
        await db.upsert_config(project_id="gc-proj", slug="ref", title="T", brief="B")
        row = await db.get_config("gc-proj", "ref")
        assert row is not None
        assert row["slug"] == "ref"

    async def test_get_config_returns_none_for_missing(self, db):
        await self._setup_project(db, "gcm-proj")
        row = await db.get_config("gcm-proj", "nonexistent")
        assert row is None

    # --- get_config_by_id ---

    async def test_get_config_by_id_returns_row(self, db):
        await self._setup_project(db, "gci-proj")
        created = await db.upsert_config(project_id="gci-proj", slug="by-id", title="T", brief="B")
        row = await db.get_config_by_id(created["id"])
        assert row is not None
        assert row["id"] == created["id"]

    async def test_get_config_by_id_returns_none_for_missing(self, db):
        row = await db.get_config_by_id("nonexistent-id")
        assert row is None

    # --- list_configs ---

    async def test_list_configs_ordered_by_slug(self, db):
        await self._setup_project(db, "lc-proj")
        for slug in ("zebra", "alpha", "middle"):
            await db.upsert_config(project_id="lc-proj", slug=slug, title="T", brief="B")
        rows = await db.list_configs("lc-proj")
        slugs = [r["slug"] for r in rows]
        assert slugs == sorted(slugs), "list_configs must be ordered by slug ASC"

    async def test_list_configs_filters_by_project(self, db):
        await self._setup_project(db, "lf-proj-a")
        await self._setup_project(db, "lf-proj-b")
        await db.upsert_config(project_id="lf-proj-a", slug="doc-a", title="T", brief="B")
        await db.upsert_config(project_id="lf-proj-b", slug="doc-b", title="T", brief="B")
        rows = await db.list_configs("lf-proj-a")
        assert len(rows) == 1
        assert rows[0]["slug"] == "doc-a"

    # --- delete_config_row ---

    async def test_delete_config_row_returns_true(self, db):
        await self._setup_project(db, "dc-proj")
        row = await db.upsert_config(project_id="dc-proj", slug="to-delete", title="T", brief="B")
        result = await db.delete_config_row(row["id"])
        assert result is True
        assert await db.get_config_by_id(row["id"]) is None

    async def test_delete_config_row_returns_false_for_missing(self, db):
        result = await db.delete_config_row("no-such-id")
        assert result is False

    # --- update_config_meta ---

    async def test_update_config_meta_partial_update(self, db):
        await self._setup_project(db, "um-proj")
        await self._setup_task(db, "um-proj", "um-proj/t1")
        row = await db.upsert_config(project_id="um-proj", slug="meta-doc", title="T", brief="B")
        await db.update_config_meta(
            row["id"],
            last_seen_sha="abc123",
            last_regen_at="2026-04-01T00:00:00Z",
            last_regen_task_id="um-proj/t1",
        )
        updated = await db.get_config_by_id(row["id"])
        assert updated["last_seen_sha"] == "abc123"
        assert updated["last_regen_at"] == "2026-04-01T00:00:00Z"
        assert updated["last_regen_task_id"] == "um-proj/t1"

    async def test_update_config_meta_none_fields_not_touched(self, db):
        await self._setup_project(db, "umn-proj")
        row = await db.upsert_config(project_id="umn-proj", slug="partial", title="T", brief="B")
        # Set a value first
        await db.update_config_meta(row["id"], last_seen_sha="sha-before")
        # Update only last_regen_at; last_seen_sha must remain
        await db.update_config_meta(row["id"], last_regen_at="2026-04-02T00:00:00Z")
        updated = await db.get_config_by_id(row["id"])
        assert updated["last_seen_sha"] == "sha-before"
        assert updated["last_regen_at"] == "2026-04-02T00:00:00Z"

    async def test_update_config_meta_noop_when_all_none(self, db):
        await self._setup_project(db, "noop-proj")
        row = await db.upsert_config(project_id="noop-proj", slug="noop", title="T", brief="B")
        # Should not raise even when all fields are None
        await db.update_config_meta(row["id"])
        updated = await db.get_config_by_id(row["id"])
        assert updated is not None

    # --- insert_run + list_runs (JSON round-trip) ---

    async def test_insert_run_json_roundtrip(self, db):
        await self._setup_project(db, "ir-proj")
        await self._setup_task(db, "ir-proj", "ir-proj/t1")
        run = await db.insert_run(
            project_id="ir-proj",
            task_id="ir-proj/t1",
            commit_sha="deadbeef",
            outcome="updated",
            slugs_changed=["api-ref", "overview"],
            slugs_unchanged=["changelog"],
        )
        assert run["outcome"] == "updated"
        assert run["slugs_changed"] == ["api-ref", "overview"]
        assert run["slugs_unchanged"] == ["changelog"]
        assert run["id"] is not None

    async def test_list_runs_most_recent_first(self, db):
        await self._setup_project(db, "lr-proj")
        await self._setup_task(db, "lr-proj", "lr-proj/t1")
        await self._setup_task(db, "lr-proj", "lr-proj/t2")
        run1 = await db.insert_run(
            project_id="lr-proj", task_id="lr-proj/t1",
            commit_sha=None, outcome="unchanged",
            slugs_changed=[], slugs_unchanged=["doc"],
        )
        run2 = await db.insert_run(
            project_id="lr-proj", task_id="lr-proj/t2",
            commit_sha=None, outcome="updated",
            slugs_changed=["doc"], slugs_unchanged=[],
        )
        runs = await db.list_runs("lr-proj")
        # Most recent first — run2 was inserted after run1
        assert runs[0]["id"] == run2["id"]
        assert runs[1]["id"] == run1["id"]

    async def test_list_runs_decodes_slug_json(self, db):
        await self._setup_project(db, "ld-proj")
        await self._setup_task(db, "ld-proj", "ld-proj/t1")
        await db.insert_run(
            project_id="ld-proj", task_id="ld-proj/t1",
            commit_sha=None, outcome="failed",
            slugs_changed=[], slugs_unchanged=[],
            error_message="Oops",
        )
        runs = await db.list_runs("ld-proj")
        assert isinstance(runs[0]["slugs_changed"], list)
        assert isinstance(runs[0]["slugs_unchanged"], list)

    async def test_list_runs_limit(self, db):
        await self._setup_project(db, "lim-proj")
        for i in range(5):
            tid = f"lim-proj/t{i}"
            await self._setup_task(db, "lim-proj", tid)
            await db.insert_run(
                project_id="lim-proj", task_id=tid,
                commit_sha=None, outcome="unchanged",
                slugs_changed=[], slugs_unchanged=[],
            )
        runs = await db.list_runs("lim-proj", limit=3)
        assert len(runs) == 3

    # --- get_runs_by_task ---

    async def test_get_runs_by_task(self, db):
        await self._setup_project(db, "grt-proj")
        await self._setup_task(db, "grt-proj", "grt-proj/t1")
        await self._setup_task(db, "grt-proj", "grt-proj/t2")
        await db.insert_run(
            project_id="grt-proj", task_id="grt-proj/t1",
            commit_sha=None, outcome="updated", slugs_changed=["x"], slugs_unchanged=[],
        )
        await db.insert_run(
            project_id="grt-proj", task_id="grt-proj/t2",
            commit_sha=None, outcome="unchanged", slugs_changed=[], slugs_unchanged=["y"],
        )
        runs = await db.get_runs_by_task("grt-proj/t1")
        assert len(runs) == 1
        assert runs[0]["task_id"] == "grt-proj/t1"

    # --- get_latest_regen_at ---

    async def test_get_latest_regen_at_none_for_no_configs(self, db):
        await self._setup_project(db, "lra-proj")
        result = await db.get_latest_regen_at("lra-proj")
        assert result is None

    async def test_get_latest_regen_at_returns_max(self, db):
        await self._setup_project(db, "lra2-proj")
        r1 = await db.upsert_config(project_id="lra2-proj", slug="doc-a", title="T", brief="B")
        r2 = await db.upsert_config(project_id="lra2-proj", slug="doc-b", title="T", brief="B")
        await db.update_config_meta(r1["id"], last_regen_at="2026-03-01T00:00:00Z")
        await db.update_config_meta(r2["id"], last_regen_at="2026-04-01T00:00:00Z")
        latest = await db.get_latest_regen_at("lra2-proj")
        assert latest == "2026-04-01T00:00:00Z"

    # --- has_inflight_tagged_task ---

    async def test_has_inflight_tagged_task_true_when_working(self, db):
        await self._setup_project(db, "hit-proj")
        await self._setup_task(db, "hit-proj", "hit-proj/t1")
        await db.update_task("hit-proj/t1", status="working")
        await db.set_task_tags("hit-proj/t1", ["living-docs"])
        result = await db.has_inflight_tagged_task("hit-proj", "living-docs")
        assert result is True

    async def test_has_inflight_tagged_task_false_for_different_tag(self, db):
        await self._setup_project(db, "hft-proj")
        await self._setup_task(db, "hft-proj", "hft-proj/t1")
        await db.set_task_tags("hft-proj/t1", ["living-docs"])
        result = await db.has_inflight_tagged_task("hft-proj", "other-tag")
        assert result is False

    async def test_has_inflight_tagged_task_false_when_no_tasks(self, db):
        await self._setup_project(db, "hfn-proj")
        result = await db.has_inflight_tagged_task("hfn-proj", "living-docs")
        assert result is False

    # --- FK cascade: projects → configs + runs ---

    async def test_fk_cascade_delete_project_removes_configs_and_runs(self, db):
        await self._setup_project(db, "fk-proj")
        await self._setup_task(db, "fk-proj", "fk-proj/t1")
        await db.upsert_config(project_id="fk-proj", slug="doc", title="T", brief="B")
        await db.insert_run(
            project_id="fk-proj", task_id="fk-proj/t1",
            commit_sha=None, outcome="updated",
            slugs_changed=["doc"], slugs_unchanged=[],
        )

        configs_before = await db.list_configs("fk-proj")
        runs_before = await db.list_runs("fk-proj")
        assert len(configs_before) == 1
        assert len(runs_before) == 1

        await db.delete_project("fk-proj")

        configs_after = await db.list_configs("fk-proj")
        runs_after = await db.list_runs("fk-proj")
        assert configs_after == [], "FK cascade must remove configs on project delete"
        assert runs_after == [], "FK cascade must remove runs on project delete"
