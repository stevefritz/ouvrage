"""Tests for rename_project — atomic cascade rename of project ID across DB and disk."""

import json
import os
import tempfile
from unittest.mock import patch

import pytest

import switchboard.db as db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _create_project(project_id="old-project", working_dir=None):
    wd = working_dir or f"/tmp/{project_id}"
    return await db.create_project(
        id=project_id,
        repo="https://github.com/org/repo.git",
        working_dir=wd,
        default_branch="main",
    )


async def _create_task(task_id, project_id, status="ready", **kw):
    task = await db.create_task(id=task_id, project_id=project_id,
                                goal="test goal", **kw)
    if status != "ready":
        task = await db.update_task(task_id, status=status)
    return task


# ---------------------------------------------------------------------------
# DB-level rename_project
# ---------------------------------------------------------------------------

class TestRenameProjectDB:
    """Tests against db.rename_project() directly."""

    async def test_basic_rename(self, db):
        await _create_project("old-project")
        result = await db.rename_project("old-project", "new-project")

        assert result["id"] == "new-project"
        assert await db.get_project("old-project") is None
        assert await db.get_project("new-project") is not None

    async def test_raises_if_old_not_found(self, db):
        with pytest.raises(ValueError, match="not found"):
            await db.rename_project("nonexistent", "other")

    async def test_raises_if_new_id_exists(self, db):
        await _create_project("proj-a")
        await _create_project("proj-b")
        with pytest.raises(ValueError, match="already exists"):
            await db.rename_project("proj-a", "proj-b")

    async def test_raises_if_active_tasks(self, db):
        await _create_project("proj-active")
        for status in ("working", "dispatching", "testing", "reviewing"):
            await _create_task(f"proj-active/{status}-task", "proj-active", status=status)

        with pytest.raises(ValueError, match="active"):
            await db.rename_project("proj-active", "new-active")

    async def test_inactive_tasks_dont_block(self, db):
        await _create_project("proj-inactive")
        for status in ("ready", "completed", "failed", "paused"):
            await _create_task(f"proj-inactive/{status}-task", "proj-inactive", status=status)

        result = await db.rename_project("proj-inactive", "new-inactive")
        assert result["id"] == "new-inactive"

    async def test_invalid_new_id_format(self, db):
        await _create_project("proj-x")
        with pytest.raises(ValueError, match="invalid"):
            await db.rename_project("proj-x", "My Project!")

    async def test_invalid_new_id_with_slash(self, db):
        await _create_project("proj-slash")
        with pytest.raises(ValueError, match="invalid"):
            await db.rename_project("proj-slash", "a/b")

    async def test_tasks_project_id_updated(self, db):
        await _create_project("proj-tasks")
        await _create_task("proj-tasks/task-1", "proj-tasks")
        await _create_task("proj-tasks/task-2", "proj-tasks")

        await db.rename_project("proj-tasks", "renamed-tasks")

        tasks = await db.list_tasks(project_id="renamed-tasks")
        assert len(tasks) == 2
        assert all(t["project_id"] == "renamed-tasks" for t in tasks)
        # Old project_id should have no tasks
        assert await db.list_tasks(project_id="proj-tasks") == []

    async def test_task_ids_updated(self, db):
        await _create_project("proj-ids")
        await _create_task("proj-ids/task-a", "proj-ids")
        await _create_task("proj-ids/task-b", "proj-ids")

        await db.rename_project("proj-ids", "newids")

        task = await db.get_task("newids/task-a")
        assert task is not None
        assert task["id"] == "newids/task-a"
        assert await db.get_task("proj-ids/task-a") is None

    async def test_task_checklist_updated(self, db):
        await _create_project("proj-checklist")
        task = await _create_task("proj-checklist/my-task", "proj-checklist")
        await db.create_checklist_items("proj-checklist/my-task", ["item 1", "item 2"])

        await db.rename_project("proj-checklist", "renamed-cl")

        # Checklist items should exist under new task_id
        from switchboard.db.connection import get_db as _get_db
        async with _get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT * FROM task_checklist WHERE task_id LIKE 'renamed-cl/%'"
            )
        assert len(rows) == 2
        assert all(r["task_id"].startswith("renamed-cl/") for r in rows)

    async def test_messages_updated(self, db):
        await _create_project("proj-msgs")
        await _create_task("proj-msgs/msg-task", "proj-msgs")
        await db.post_task_message(
            task_id="proj-msgs/msg-task",
            author="test",
            content="hello",
            type="progress",
        )

        await db.rename_project("proj-msgs", "renamed-msgs")

        result = await db.read_task_messages("renamed-msgs/msg-task")
        msgs = result["messages"]
        assert len(msgs) > 0
        assert all(m["task_id"] == "renamed-msgs/msg-task" for m in msgs)

    async def test_conversations_updated(self, db):
        await _create_project("proj-convs")
        await db.create_conversation(
            id="my-conv", project="proj-convs", goal="test conv"
        )

        await db.rename_project("proj-convs", "renamed-convs")

        convs = await db.list_conversations(project="renamed-convs")
        assert len(convs) == 1
        assert convs[0]["project"] == "renamed-convs"

    async def test_depends_on_updated(self, db):
        await _create_project("proj-chain")
        await _create_task("proj-chain/task-a", "proj-chain")
        await _create_task("proj-chain/task-b", "proj-chain",
                           depends_on="proj-chain/task-a")

        await db.rename_project("proj-chain", "new-chain")

        task_b = await db.get_task("new-chain/task-b")
        assert task_b["depends_on"] == "new-chain/task-a"

    async def test_working_dir_updated_in_db(self, db):
        await _create_project("proj-wd", working_dir="/tmp/proj-wd")
        await db.rename_project("proj-wd", "new-wd")

        project = await db.get_project("new-wd")
        assert project["working_dir"] == "/tmp/new-wd"

    async def test_components_updated(self, db):
        await _create_project("proj-comp")
        await db.create_component(
            id="comp-1",
            project_id="proj-comp",
            name="My Component",
            created_by=None,
        )

        await db.rename_project("proj-comp", "new-comp")

        comps = await db.list_components(project_id="new-comp")
        assert len(comps) == 1
        assert comps[0]["project_id"] == "new-comp"

    async def test_audit_log_updated(self, db):
        await _create_project("proj-audit")
        await _create_task("proj-audit/audit-task", "proj-audit")
        await db.write_audit_log(
            task_id="proj-audit/audit-task",
            action="cancelled",
            triggered_by="test",
        )

        await db.rename_project("proj-audit", "new-audit")

        from switchboard.db.connection import get_db as _get_db
        async with _get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT * FROM task_audit_log WHERE task_id LIKE 'new-audit/%'"
            )
        assert len(rows) >= 1
        assert all(r["task_id"] == "new-audit/audit-task" for r in rows)

    async def test_subtasks_updated(self, db):
        await _create_project("proj-sub")
        await _create_task("proj-sub/sub-task", "proj-sub")
        await db.create_subtask(
            id="proj-sub/sub-task/review-1",
            task_id="proj-sub/sub-task",
            type="review",
            prompt="review me",
        )

        await db.rename_project("proj-sub", "new-sub")

        sub = await db.get_subtask("new-sub/sub-task/review-1")
        assert sub is not None
        assert sub["task_id"] == "new-sub/sub-task"
        assert sub["id"] == "new-sub/sub-task/review-1"

    async def test_files_project_id_updated(self, db):
        """files.project_id (separate from task_id) must be updated on rename."""
        await _create_project("proj-files")
        await _create_task("proj-files/file-task", "proj-files")

        # Create a file with project_id set directly (as promote_task_file does)
        from switchboard.db.files import create_file
        await create_file(
            id="file-001",
            filename="report.txt",
            stored_path="/tmp/report.txt",
            mime_type="text/plain",
            size_bytes=42,
            uploaded_by=None,
            task_id="proj-files/file-task",
            project_id="proj-files",
        )

        await db.rename_project("proj-files", "renamed-files")

        from switchboard.db.files import get_file
        f = await get_file("file-001")
        assert f is not None
        assert f["project_id"] == "renamed-files", (
            f"files.project_id not updated: got {f['project_id']!r}"
        )
        assert f["task_id"] == "renamed-files/file-task"


# ---------------------------------------------------------------------------
# Reject active tasks — various statuses
# ---------------------------------------------------------------------------

class TestRenameRejectsActiveStatuses:
    @pytest.mark.parametrize("status", ["working", "dispatching", "testing", "reviewing"])
    async def test_rejects_active_status(self, db, status):
        await _create_project(f"active-proj-{status}")
        await _create_task(f"active-proj-{status}/t", f"active-proj-{status}", status=status)

        with pytest.raises(ValueError, match="active"):
            await db.rename_project(f"active-proj-{status}", "new-id-x")


# ---------------------------------------------------------------------------
# Disk rename
# ---------------------------------------------------------------------------

class TestDiskRename:
    async def test_disk_directory_renamed(self, db):
        with tempfile.TemporaryDirectory() as base:
            old_dir = os.path.join(base, "old-disk-proj")
            os.makedirs(old_dir)
            # Write a file to verify the dir is renamed (not just recreated)
            marker = os.path.join(old_dir, "marker.txt")
            with open(marker, "w") as f:
                f.write("contents")

            await _create_project("old-disk-proj", working_dir=old_dir)

            # Use the MCP handler so the disk rename is exercised
            from switchboard.server.handlers.projects import _handle_rename_project
            result = await _handle_rename_project({
                "project_id": "old-disk-proj",
                "new_id": "new-disk-proj",
            })

            new_dir = os.path.join(base, "new-disk-proj")
            assert not os.path.exists(old_dir), "Old directory should be gone"
            assert os.path.exists(new_dir), "New directory should exist"
            assert os.path.exists(os.path.join(new_dir, "marker.txt")), "Contents preserved"
            assert result.get("error") is None or "error" not in result

    async def test_disk_rename_skipped_if_no_dir(self, db):
        """If working dir doesn't exist on disk, rename still succeeds (DB only)."""
        await _create_project("no-disk-proj", working_dir="/tmp/no-disk-proj-nonexistent")

        from switchboard.server.handlers.projects import _handle_rename_project
        result = await _handle_rename_project({
            "project_id": "no-disk-proj",
            "new_id": "no-disk-renamed",
        })

        assert "error" not in result
        assert result["id"] == "no-disk-renamed"

    async def test_disk_rename_failure_returns_warning(self, db):
        """If disk rename fails, DB is committed but response contains warning."""
        await _create_project("fail-disk-proj", working_dir="/tmp/fail-disk")

        from switchboard.server.handlers.projects import _handle_rename_project
        with patch("switchboard.server.handlers.projects.os") as mock_os:
            mock_os.path.dirname.return_value = "/tmp"
            mock_os.path.join.side_effect = os.path.join
            mock_os.path.isdir.return_value = True
            mock_os.rename.side_effect = OSError("Permission denied")

            result = await _handle_rename_project({
                "project_id": "fail-disk-proj",
                "new_id": "fail-disk-renamed",
            })

        assert "warning" in result
        assert "Permission denied" in result["warning"]
        # DB rename still committed
        assert await db.get_project("fail-disk-renamed") is not None


# ---------------------------------------------------------------------------
# MCP handler (server/handlers/projects.py)
# ---------------------------------------------------------------------------

class TestRenameMCPHandler:
    async def test_handler_returns_renamed_project(self, db):
        await _create_project("mcp-old")
        from switchboard.server.handlers.projects import _handle_rename_project
        result = await _handle_rename_project({"project_id": "mcp-old", "new_id": "mcp-new"})

        assert "error" not in result
        assert result["id"] == "mcp-new"
        assert result["renamed"] is True
        assert result["old_id"] == "mcp-old"

    async def test_handler_returns_error_for_active_tasks(self, db):
        await _create_project("mcp-active")
        await _create_task("mcp-active/t", "mcp-active", status="working")

        from switchboard.server.handlers.projects import _handle_rename_project
        result = await _handle_rename_project({"project_id": "mcp-active", "new_id": "mcp-done"})
        assert "error" in result
        assert "active" in result["error"]

    async def test_handler_returns_error_for_missing_project(self, db):
        from switchboard.server.handlers.projects import _handle_rename_project
        result = await _handle_rename_project({"project_id": "ghost", "new_id": "phantom"})
        assert "error" in result
        assert "not found" in result["error"]

    async def test_handler_returns_error_for_invalid_new_id(self, db):
        await _create_project("mcp-fmt")
        from switchboard.server.handlers.projects import _handle_rename_project
        result = await _handle_rename_project({"project_id": "mcp-fmt", "new_id": "INVALID!"})
        assert "error" in result


# ---------------------------------------------------------------------------
# Dashboard API endpoint
# ---------------------------------------------------------------------------

def _make_scope(path, method="POST", user_id=1):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "session_user": {"id": user_id, "email": "owner@localhost", "name": "Owner", "role": "owner"},
    }


def _make_receive(body=None):
    if body is None:
        raw = b""
    elif isinstance(body, dict):
        raw = json.dumps(body).encode()
    else:
        raw = body

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return receive


class _Capture:
    def __init__(self):
        self.status = None
        self.body = b""

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")

    def json(self):
        return json.loads(self.body)


class TestRenameDashboardAPI:
    async def _create_proj(self, proj_id="dash-old"):
        from switchboard.dashboard.api import handle_request
        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()
        with patch("switchboard.dashboard.api._WORKTREE_BASE", "/work"):
            with patch("switchboard.db.get_instance_github_pat", return_value="ghp_test"):
                with patch("switchboard.server.handlers.projects._validate_github_pat_for_repo", return_value=None):
                    await handle_request(scope, _make_receive({
                        "id": proj_id,
                        "repo": "https://github.com/org/repo.git",
                        "model": "sonnet",
                        "review_model": "opus",
                        "auto_test": True,
                        "auto_review": True,
                        "auto_pr": False,
                        "auto_merge": False,
                        "max_turns": 100,
                        "max_wall_clock": 30,
                    }), resp)
        assert resp.status == 201, resp.json()
        return resp.json()["id"]

    async def test_rename_via_api(self, db):
        from switchboard.dashboard.api import handle_request
        proj_id = await self._create_proj("api-old")

        scope = _make_scope(f"/dashboard/api/projects/{proj_id}/rename")
        resp = _Capture()
        await handle_request(scope, _make_receive({"new_id": "api-new"}), resp)

        assert resp.status == 200, resp.body
        data = resp.json()
        assert data["id"] == "api-new"
        assert data["renamed"] is True
        assert data["old_id"] == "api-old"

    async def test_rename_api_not_found(self, db):
        from switchboard.dashboard.api import handle_request
        scope = _make_scope("/dashboard/api/projects/ghost-proj/rename")
        resp = _Capture()
        await handle_request(scope, _make_receive({"new_id": "anything"}), resp)
        assert resp.status == 404

    async def test_rename_api_missing_new_id(self, db):
        from switchboard.dashboard.api import handle_request
        proj_id = await self._create_proj("api-need-id")

        scope = _make_scope(f"/dashboard/api/projects/{proj_id}/rename")
        resp = _Capture()
        await handle_request(scope, _make_receive({}), resp)
        assert resp.status == 400
        assert "new_id" in resp.json()["error"]

    async def test_rename_api_conflict_with_active_task(self, db):
        from switchboard.dashboard.api import handle_request
        proj_id = await self._create_proj("api-busy")
        await _create_task(f"{proj_id}/t", proj_id, status="working")

        scope = _make_scope(f"/dashboard/api/projects/{proj_id}/rename")
        resp = _Capture()
        await handle_request(scope, _make_receive({"new_id": "api-free"}), resp)
        assert resp.status == 409

    async def test_rename_api_unauthenticated(self, db):
        from switchboard.dashboard.api import handle_request
        scope = _make_scope("/dashboard/api/projects/any/rename")
        scope["session_user"] = {}
        resp = _Capture()
        await handle_request(scope, _make_receive({"new_id": "x"}), resp)
        assert resp.status == 401
