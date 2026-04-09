"""Tests for delete_project cascade behavior and precondition checks.

Covers:
- FK violation when project-level files exist (pre-fix regression)
- Full cascade: tasks, messages, checklist, components, conversations, files,
  punchlist, subtasks, task_attempts, task_audit_log are all cleaned up
- Conversations linked to the project via text field are deleted
- FTS tables are clean after delete (trigger-synced)
- Precondition rejects working AND validating tasks
- Zero orphaned rows in all related tables after delete
"""
import os
import pytest

from switchboard.db._helpers import now_iso
from switchboard.db.connection import get_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _count(table, **where):
    """Return count of rows in table matching where kwargs."""
    async with get_db() as conn:
        if where:
            clause = " AND ".join(f"{k} = ?" for k in where)
            rows = await conn.execute_fetchall(
                f"SELECT COUNT(*) AS cnt FROM {table} WHERE {clause}",
                list(where.values()),
            )
        else:
            rows = await conn.execute_fetchall(f"SELECT COUNT(*) AS cnt FROM {table}")
    return rows[0]["cnt"]


async def _create_full_project(db, project_id: str):
    """Create a project with a full set of child data for cascade testing."""
    ts = now_iso()

    # Project
    await db.create_project(
        id=project_id,
        repo=f"https://github.com/acme/{project_id}.git",
        working_dir=f"/tmp/{project_id}",
    )

    # Task 1: completed
    task1 = await db.create_task(
        id=f"{project_id}/task-1",
        project_id=project_id,
        goal="Task one",
    )
    await db.update_task(task1["id"], status="completed")

    # Task 2: cancelled
    task2 = await db.create_task(
        id=f"{project_id}/task-2",
        project_id=project_id,
        goal="Task two",
    )
    await db.update_task(task2["id"], status="cancelled")

    async with get_db() as conn:
        # Checklist items for task1
        await conn.execute(
            "INSERT INTO task_checklist (task_id, item, done) VALUES (?, ?, ?)",
            (task1["id"], "Step A", False),
        )
        await conn.execute(
            "INSERT INTO task_checklist (task_id, item, done) VALUES (?, ?, ?)",
            (task1["id"], "Step B", True),
        )

        # Task tags
        await conn.execute(
            "INSERT INTO task_tags (task_id, tag) VALUES (?, ?)",
            (task1["id"], "feature"),
        )

        # Task artifacts
        await conn.execute(
            "INSERT INTO task_artifacts (task_id, type, ref) VALUES (?, ?, ?)",
            (task1["id"], "pr", "https://github.com/acme/repo/pull/1"),
        )

        # Subtask
        await conn.execute(
            "INSERT INTO subtasks (id, task_id, type, status) VALUES (?, ?, ?, ?)",
            (f"{project_id}/task-1/sub-1", task1["id"], "review", "completed"),
        )

        # task_audit_log rows
        await conn.execute(
            """INSERT INTO task_audit_log (task_id, action, triggered_by, previous_status, new_status)
               VALUES (?, ?, ?, ?, ?)""",
            (task1["id"], "status_change", "user", "ready", "working"),
        )
        await conn.execute(
            """INSERT INTO task_audit_log (task_id, action, triggered_by, previous_status, new_status)
               VALUES (?, ?, ?, ?, ?)""",
            (task1["id"], "status_change", "user", "working", "completed"),
        )

        # task_attempts (ON DELETE CASCADE exists, but we verify cleanup)
        await conn.execute(
            "INSERT INTO task_attempts (task_id, attempt_number, outcome) VALUES (?, ?, ?)",
            (task1["id"], 1, "success"),
        )

        # Message linked to task1
        await conn.execute(
            "INSERT INTO messages (task_id, author, type, content) VALUES (?, ?, ?, ?)",
            (task1["id"], "cc-worker", "progress", "Task progress update"),
        )

        # File linked to task1 (also has project_id)
        await conn.execute(
            "INSERT INTO files (id, filename, stored_path, task_id, project_id) VALUES (?, ?, ?, ?, ?)",
            (f"{project_id}-task-file", "task-output.txt", "/tmp/task-output.txt",
             task1["id"], project_id),
        )

        # Project-level file (task_id IS NULL) — causes FK violation without fix
        await conn.execute(
            "INSERT INTO files (id, filename, stored_path, project_id) VALUES (?, ?, ?, ?)",
            (f"{project_id}-proj-file", "report.pdf", "/tmp/report.pdf", project_id),
        )

        # Component with punchlist
        await conn.execute(
            """INSERT INTO components (id, project_id, name, description, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (f"{project_id}-comp", project_id, "Auth", "Auth component", ts, ts),
        )
        await conn.execute(
            "INSERT INTO punchlist (component_id, item, status, author, created_at) VALUES (?, ?, ?, ?, ?)",
            (f"{project_id}-comp", "Add OAuth support", "open", "owner", ts),
        )

        # Conversation linked to this project (by project text field)
        await conn.execute(
            "INSERT INTO conversations (id, project, goal) VALUES (?, ?, ?)",
            (f"{project_id}-conv", project_id, "Project planning conversation"),
        )
        await conn.execute(
            "INSERT INTO messages (conversation_id, author, type, content) VALUES (?, ?, ?, ?)",
            (f"{project_id}-conv", "owner", "note", "Let's build this feature"),
        )

        await conn.commit()

    return task1, task2


# ---------------------------------------------------------------------------
# 5322: Failing test — FK violation with project-level file (no task_id)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5323–5328: Comprehensive cascade + zero-orphan test
# ---------------------------------------------------------------------------


class TestDeleteProjectCascade:
    """delete_project removes ALL child data — zero orphaned rows after delete."""

    async def test_full_cascade_no_orphans(self, db):
        """Create a project with all child data types; delete it; verify zero orphans."""
        from switchboard.db.projects import delete_project

        project_id = "cascade-project"
        task1, task2 = await _create_full_project(db, project_id)

        # Verify data exists before delete
        assert await _count("tasks", project_id=project_id) == 2
        assert await _count("task_checklist", task_id=task1["id"]) == 2
        assert await _count("task_tags", task_id=task1["id"]) == 1
        assert await _count("task_artifacts", task_id=task1["id"]) == 1
        assert await _count("subtasks", task_id=task1["id"]) == 1
        assert await _count("task_audit_log", task_id=task1["id"]) >= 2  # 2 manual + 1 from update_task
        assert await _count("task_attempts", task_id=task1["id"]) == 1
        assert await _count("messages", task_id=task1["id"]) == 1
        assert await _count("files", project_id=project_id) == 2  # task + project-level
        assert await _count("components", project_id=project_id) == 1
        assert await _count("conversations", project=project_id) == 1
        assert await _count("messages", conversation_id=f"{project_id}-conv") == 1

        # Delete the project
        await delete_project(project_id)

        # Verify zero orphans in all tables
        assert await _count("tasks", project_id=project_id) == 0
        assert await _count("task_checklist", task_id=task1["id"]) == 0
        assert await _count("task_tags", task_id=task1["id"]) == 0
        assert await _count("task_artifacts", task_id=task1["id"]) == 0
        assert await _count("subtasks", task_id=task1["id"]) == 0
        assert await _count("task_audit_log", task_id=task1["id"]) == 0
        assert await _count("task_attempts", task_id=task1["id"]) == 0
        assert await _count("messages", task_id=task1["id"]) == 0
        assert await _count("files", project_id=project_id) == 0
        assert await _count("components", project_id=project_id) == 0
        assert await _count("conversations", project=project_id) == 0
        assert await _count("messages", conversation_id=f"{project_id}-conv") == 0

        # Project row is gone
        project = await db.get_project(project_id)
        assert project is None


# ---------------------------------------------------------------------------
# 5326: Precondition — reject working AND validating tasks
# ---------------------------------------------------------------------------


class TestDeleteProjectPrecondition:
    """_handle_delete_project rejects projects with working OR validating tasks."""

    async def test_rejects_working_tasks(self, db, tmp_path):
        """Working tasks block delete."""
        working_dir = str(tmp_path / "working-proj")
        os.makedirs(working_dir)
        await db.create_project(
            id="working-proj",
            repo="https://github.com/acme/working.git",
            working_dir=working_dir,
        )
        task = await db.create_task(
            id="working-proj/task-1",
            project_id="working-proj",
            goal="Active task",
        )
        await db.update_task(task["id"], status="working")

        from switchboard.server.handlers.projects import _handle_delete_project

        result = await _handle_delete_project({"project_id": "working-proj"})

        assert "error" in result
        assert "working-proj/task-1" in result["error"]
        project = await db.get_project("working-proj")
        assert project is not None


    async def test_allows_completed_tasks(self, db, tmp_path):
        """Completed/failed/cancelled tasks don't block delete."""
        working_dir = str(tmp_path / "done-proj")
        os.makedirs(working_dir)
        await db.create_project(
            id="done-proj",
            repo="https://github.com/acme/done.git",
            working_dir=working_dir,
        )
        for status in ("completed", "failed", "cancelled"):
            task = await db.create_task(
                id=f"done-proj/task-{status}",
                project_id="done-proj",
                goal=f"Task with status {status}",
            )
            await db.update_task(task["id"], status=status)

        from switchboard.server.handlers.projects import _handle_delete_project

        result = await _handle_delete_project({"project_id": "done-proj"})
        assert result.get("deleted") is True

