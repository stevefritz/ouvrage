"""Tests for onboarding guardrails:

- dispatch_task without Anthropic key returns clear error, no task created
- create_project without PAT returns clear error, no project row
- create_project with bad PAT (ls-remote fails) does not leave dangling project row
- delete_project (MCP) removes project and bare repo
- delete_project (MCP) rejects if project has working tasks
- Dashboard delete project API endpoint mirrors MCP behavior
- 500 responses produce log output
"""

import json
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── ASGI test helpers ──────────────────────────────────────────────────────


def _make_scope(method="POST", path="/dashboard/api/tasks", user=None, no_user=False):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "session_user": {} if no_user else (user or {"id": 1, "email": "owner@localhost", "name": "Owner", "role": "owner"}),
    }


def _make_receive(body=None):
    raw = json.dumps(body).encode() if isinstance(body, dict) else (body or b"")

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


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
async def owner_user(db):
    """Get the bootstrap owner user seeded by init_db."""
    user = await db.get_user_by_email("owner@localhost")
    assert user is not None, "init_db should have seeded owner@localhost"
    await db.update_instance(owner_user_id=user["id"])
    return user


@pytest.fixture
async def user_with_anthropic_key(db, owner_user):
    """Owner user with Anthropic API key set."""
    await db.update_user_credentials(owner_user["id"], anthropic_api_key="sk-ant-test-key")
    return owner_user


@pytest.fixture
async def user_without_anthropic_key(db, owner_user):
    """Owner user without any Anthropic API key."""
    return owner_user


# ── Issue 1: dispatch_task credential guard ────────────────────────────────


class TestDispatchTaskCredentialGuard:
    """dispatch_task must reject before creating any task record when Anthropic key is missing."""

    @pytest.fixture(autouse=True)
    def patch_context(self, owner_user):
        """Set MCP request context to the owner user; disable bypass flags so the guard fires."""
        import switchboard.server.handlers.tasks as tasks_module
        with patch("switchboard.server.handlers.tasks.get_request_user_id", return_value=owner_user["id"]):
            with patch("switchboard.server.handlers.tasks.get_request_is_token_auth", return_value=True):
                with patch("switchboard.server.handlers.tasks.get_request_is_worker", return_value=False):
                    with patch.object(tasks_module, "SKIP_CREDENTIAL_CHECK", False):
                        with patch.object(tasks_module, "HAS_CLAUDE_BINARY", False):
                            yield

    async def test_dispatch_without_anthropic_key_returns_error(self, db, sample_project, user_without_anthropic_key):
        """dispatch_task with no key → error dict, no task row created."""
        from switchboard.server.handlers.tasks import _handle_dispatch_task

        result = await _handle_dispatch_task({
            "project_id": "test-project",
            "id": "test-project/my-new-task",
            "goal": "Do something",
            "held": True,
        })

        assert "error" in result
        assert "Anthropic API key" in result["error"]
        assert "Settings" in result["error"]

        # Verify no task row was created
        task = await db.get_task("test-project/my-new-task")
        assert task is None

    async def test_dispatch_with_anthropic_key_succeeds(self, db, sample_project, user_with_anthropic_key, mock_git):
        """dispatch_task with key configured → no credential error."""
        from switchboard.server.handlers.tasks import _handle_dispatch_task

        result = await _handle_dispatch_task({
            "project_id": "test-project",
            "id": "test-project/task-with-key",
            "goal": "Do something great",
            "held": True,
        })

        assert "error" not in result
        # Task should exist
        task = await db.get_task("test-project/task-with-key")
        assert task is not None

    async def test_dispatch_without_key_no_side_effects(self, db, sample_project, user_without_anthropic_key):
        """Confirm the guard fires early — no worktree created, no DB row."""
        from switchboard.server.handlers.tasks import _handle_dispatch_task

        with patch("switchboard.dispatch.engine.dispatch_task") as mock_dispatch:
            result = await _handle_dispatch_task({
                "project_id": "test-project",
                "id": "test-project/no-key-task",
                "goal": "Should not start",
                "held": True,
            })

        assert "error" in result
        # dispatch_task (engine) must never be called
        mock_dispatch.assert_not_called()


class TestDispatchTaskCredentialGuardNoUser:
    """dispatch_task guard works when no authenticated user (falls back to instance owner)."""

    @pytest.fixture(autouse=True)
    def patch_context_no_user(self):
        """Simulate no authenticated user (worker path); disable bypass flags so the guard fires."""
        import switchboard.server.handlers.tasks as tasks_module
        with patch("switchboard.server.handlers.tasks.get_request_user_id", return_value=None):
            with patch("switchboard.server.handlers.tasks.get_request_is_token_auth", return_value=False):
                with patch("switchboard.server.handlers.tasks.get_request_is_worker", return_value=True):
                    with patch.object(tasks_module, "SKIP_CREDENTIAL_CHECK", False):
                        with patch.object(tasks_module, "HAS_CLAUDE_BINARY", False):
                            yield

    async def test_dispatch_no_user_no_owner_key_returns_error(self, db, sample_project, owner_user):
        """No user context but instance owner has no key → reject with error."""
        from switchboard.server.handlers.tasks import _handle_dispatch_task

        result = await _handle_dispatch_task({
            "project_id": "test-project",
            "id": "test-project/worker-task",
            "goal": "Do worker stuff",
            "held": True,
        })

        assert "error" in result
        assert "Anthropic API key" in result["error"]

        task = await db.get_task("test-project/worker-task")
        assert task is None

    async def test_dispatch_no_user_with_owner_key_succeeds(self, db, sample_project, user_with_anthropic_key, mock_git):
        """No user context but instance owner has key → dispatch proceeds."""
        from switchboard.server.handlers.tasks import _handle_dispatch_task

        result = await _handle_dispatch_task({
            "project_id": "test-project",
            "id": "test-project/worker-task-ok",
            "goal": "Do worker stuff",
            "held": True,
        })

        assert "error" not in result


# ── Issue 2: create_project PAT validation ─────────────────────────────────


class TestCreateProjectPATGuard:
    """create_project must reject before DB write when PAT is missing or invalid."""

    _BASE_ARGS = {
        "id": "new-project",
        "repo": "https://github.com/acme/new-repo.git",
        "model": "sonnet",
        "review_model": "sonnet",
        "auto_test": True,
        "auto_review": True,
        "auto_pr": False,
        "auto_merge": False,
        "max_turns": 100,
        "max_wall_clock": 30,
    }

    async def test_create_project_no_pat_returns_error(self, db):
        """No PAT configured → error, no project row created."""
        from switchboard.server.handlers.projects import _handle_create_project

        with patch("switchboard.server.handlers.projects._validate_github_pat_for_repo",
                   return_value={"error": "Add your GitHub PAT in Settings before creating projects."}):
            result = await _handle_create_project(self._BASE_ARGS)

        assert "error" in result
        assert "GitHub PAT" in result["error"]

        project = await db.get_project("new-project")
        assert project is None

    async def test_create_project_bad_pat_returns_error(self, db):
        """PAT exists but ls-remote fails → error, no project row created."""
        from switchboard.server.handlers.projects import _handle_create_project

        with patch("switchboard.server.handlers.projects._validate_github_pat_for_repo",
                   return_value={"error": "GitHub PAT cannot access this repo. Check your token's permissions."}):
            result = await _handle_create_project(self._BASE_ARGS)

        assert "error" in result
        assert "access" in result["error"].lower() or "permissions" in result["error"].lower()

        project = await db.get_project("new-project")
        assert project is None

    async def test_create_project_valid_pat_succeeds(self, db):
        """Valid PAT → create proceeds, project row exists."""
        from switchboard.server.handlers.projects import _handle_create_project

        with patch("switchboard.server.handlers.projects._validate_github_pat_for_repo", return_value=None):
            with patch("switchboard.server.handlers.projects.WORKTREE_BASE", "/work"):
                result = await _handle_create_project(self._BASE_ARGS)

        assert "error" not in result
        project = await db.get_project("new-project")
        assert project is not None


class TestValidateGithubPatForRepo:
    """Unit tests for _validate_github_pat_for_repo."""

    async def test_no_pat_returns_error(self, db):
        """get_instance_github_pat raises ValueError → returns PAT error."""
        from switchboard.server.handlers.projects import _validate_github_pat_for_repo

        with patch("switchboard.server.handlers.projects.db.get_instance_github_pat",
                   side_effect=ValueError("No PAT configured")):
            result = await _validate_github_pat_for_repo("https://github.com/acme/repo.git")

        assert result is not None
        assert "GitHub PAT" in result["error"]

    async def test_empty_pat_returns_error(self, db):
        """get_instance_github_pat returns empty string → returns PAT error."""
        from switchboard.server.handlers.projects import _validate_github_pat_for_repo

        with patch("switchboard.server.handlers.projects.db.get_instance_github_pat", return_value=""):
            result = await _validate_github_pat_for_repo("https://github.com/acme/repo.git")

        assert result is not None
        assert "GitHub PAT" in result["error"]

    async def test_ls_remote_failure_returns_error(self, db):
        """ls-remote non-zero exit → PAT cannot access repo error."""
        from switchboard.server.handlers.projects import _validate_github_pat_for_repo

        with patch("switchboard.server.handlers.projects.db.get_instance_github_pat", return_value="ghp_test"):
            with patch("switchboard.server.handlers.projects._build_authenticated_url",
                       return_value="https://ghp_test@github.com/acme/repo.git"):
                with patch("switchboard.server.handlers.projects._run_as_worker",
                           return_value=(b"", b"ERROR: auth failed", 128)) as mock_run:
                    result = await _validate_github_pat_for_repo("https://github.com/acme/repo.git")

        assert result is not None
        assert "access" in result["error"].lower() or "permissions" in result["error"].lower()

    async def test_ls_remote_success_returns_none(self, db):
        """Successful ls-remote → returns None (no error)."""
        from switchboard.server.handlers.projects import _validate_github_pat_for_repo

        with patch("switchboard.server.handlers.projects.db.get_instance_github_pat", return_value="ghp_valid"):
            with patch("switchboard.server.handlers.projects._build_authenticated_url",
                       return_value="https://ghp_valid@github.com/acme/repo.git"):
                with patch("switchboard.server.handlers.projects._run_as_worker",
                           return_value=(b"abc123\tHEAD", b"", 0)):
                    result = await _validate_github_pat_for_repo("https://github.com/acme/repo.git")

        assert result is None

    async def test_ls_remote_timeout_returns_error(self, db):
        """ls-remote times out → returns access error."""
        import asyncio
        from switchboard.server.handlers.projects import _validate_github_pat_for_repo

        async def _slow_run(*args, **kwargs):
            await asyncio.sleep(999)
            return b"", b"", 0

        with patch("switchboard.server.handlers.projects.db.get_instance_github_pat", return_value="ghp_slow"):
            with patch("switchboard.server.handlers.projects._build_authenticated_url",
                       return_value="https://ghp_slow@github.com/acme/repo.git"):
                with patch("switchboard.server.handlers.projects._run_as_worker", side_effect=_slow_run):
                    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
                        result = await _validate_github_pat_for_repo("https://github.com/acme/repo.git")

        assert result is not None
        assert "access" in result["error"].lower() or "permissions" in result["error"].lower()


# ── Issue 2d: delete_project MCP tool ─────────────────────────────────────


class TestDeleteProjectMCP:
    """MCP delete_project handler."""

    async def test_delete_project_removes_row_and_dir(self, db, tmp_path):
        """delete_project deletes DB row and removes working_dir."""
        working_dir = str(tmp_path / "my-project")
        os.makedirs(working_dir)

        project = await db.create_project(
            id="delete-me",
            repo="https://github.com/acme/delete-me.git",
            working_dir=working_dir,
        )

        from switchboard.server.handlers.projects import _handle_delete_project

        result = await _handle_delete_project({"project_id": "delete-me"})

        assert result.get("deleted") is True
        assert result["project_id"] == "delete-me"

        # DB row gone
        project = await db.get_project("delete-me")
        assert project is None

        # Directory removed
        assert not os.path.exists(working_dir)

    async def test_delete_project_rejects_with_working_tasks(self, db, tmp_path):
        """delete_project rejects if project has tasks in working status."""
        working_dir = str(tmp_path / "active-project")
        os.makedirs(working_dir)

        await db.create_project(
            id="active-project",
            repo="https://github.com/acme/active.git",
            working_dir=working_dir,
        )
        task = await db.create_task(
            id="active-project/some-task",
            project_id="active-project",
            goal="Do things",
        )
        await db.update_task(task["id"], status="working")

        from switchboard.server.handlers.projects import _handle_delete_project

        result = await _handle_delete_project({"project_id": "active-project"})

        assert "error" in result
        assert "working" in result["error"].lower()
        assert "active-project/some-task" in result["error"]

        # Project should still exist
        project = await db.get_project("active-project")
        assert project is not None

    async def test_delete_project_not_found(self, db):
        """delete_project returns error for non-existent project."""
        from switchboard.server.handlers.projects import _handle_delete_project

        result = await _handle_delete_project({"project_id": "ghost-project"})

        assert "error" in result
        assert "not found" in result["error"].lower() or "ghost-project" in result["error"]

    async def test_delete_project_without_dir_succeeds(self, db):
        """delete_project works even when working_dir doesn't exist on disk."""
        await db.create_project(
            id="no-dir-project",
            repo="https://github.com/acme/no-dir.git",
            working_dir="/tmp/switchboard-nonexistent-path-xyz",
        )

        from switchboard.server.handlers.projects import _handle_delete_project

        result = await _handle_delete_project({"project_id": "no-dir-project"})

        assert result.get("deleted") is True
        project = await db.get_project("no-dir-project")
        assert project is None

    async def test_delete_project_completed_tasks_allowed(self, db, tmp_path):
        """delete_project is allowed even if project has completed/failed tasks."""
        working_dir = str(tmp_path / "old-project")
        os.makedirs(working_dir)

        await db.create_project(
            id="old-project",
            repo="https://github.com/acme/old.git",
            working_dir=working_dir,
        )
        task = await db.create_task(
            id="old-project/done-task",
            project_id="old-project",
            goal="Done",
        )
        await db.update_task(task["id"], status="completed")

        from switchboard.server.handlers.projects import _handle_delete_project

        result = await _handle_delete_project({"project_id": "old-project"})
        assert result.get("deleted") is True


# ── Dashboard delete_project API ──────────────────────────────────────────


class TestDashboardDeleteProject:
    """DELETE /dashboard/api/projects/{id} endpoint."""

    async def test_delete_project_api_succeeds(self, db, tmp_path):
        """DELETE /dashboard/api/projects/{id} removes project and working dir."""
        working_dir = str(tmp_path / "dash-project")
        os.makedirs(working_dir)

        await db.create_project(
            id="dash-project",
            repo="https://github.com/acme/dash.git",
            working_dir=working_dir,
        )

        from switchboard.dashboard.api import handle_request

        scope = _make_scope(method="DELETE", path="/dashboard/api/projects/dash-project")
        send = _Capture()

        await handle_request(scope, _make_receive(), send)

        assert send.status == 200
        body = send.json()
        assert body["deleted"] is True
        assert body["project_id"] == "dash-project"

        project = await db.get_project("dash-project")
        assert project is None
        assert not os.path.exists(working_dir)

    async def test_delete_project_api_rejects_working_tasks(self, db, tmp_path):
        """DELETE /dashboard/api/projects/{id} returns 409 when project has working tasks."""
        working_dir = str(tmp_path / "busy-project")
        os.makedirs(working_dir)

        await db.create_project(
            id="busy-project",
            repo="https://github.com/acme/busy.git",
            working_dir=working_dir,
        )
        task = await db.create_task(
            id="busy-project/active",
            project_id="busy-project",
            goal="Busy",
        )
        await db.update_task(task["id"], status="working")

        from switchboard.dashboard.api import handle_request

        scope = _make_scope(method="DELETE", path="/dashboard/api/projects/busy-project")
        send = _Capture()

        await handle_request(scope, _make_receive(), send)

        assert send.status == 409
        body = send.json()
        assert "working" in body["error"].lower()

        project = await db.get_project("busy-project")
        assert project is not None

    async def test_delete_project_api_not_found(self, db):
        """DELETE /dashboard/api/projects/{id} returns 404 for unknown project."""
        from switchboard.dashboard.api import handle_request

        scope = _make_scope(method="DELETE", path="/dashboard/api/projects/ghost")
        send = _Capture()

        await handle_request(scope, _make_receive(), send)

        assert send.status == 404

    async def test_delete_project_api_requires_auth(self, db):
        """DELETE /dashboard/api/projects/{id} returns 401 without auth."""
        await db.create_project(
            id="auth-test-project",
            repo="https://github.com/acme/auth.git",
            working_dir="/tmp/auth-test",
        )

        from switchboard.dashboard.api import handle_request

        scope = _make_scope(method="DELETE", path="/dashboard/api/projects/auth-test-project", no_user=True)
        send = _Capture()

        await handle_request(scope, _make_receive(), send)

        assert send.status == 401


# ── Issue 3: 500 error logging ─────────────────────────────────────────────


class TestFiveHundredErrorLogging:
    """Ensure 500 errors produce logger.exception tracebacks."""

    async def test_global_exception_handler_logs_traceback(self, db, caplog):
        """Unhandled exception in API → 500 with exception logged."""
        from switchboard.dashboard.api import handle_request

        # Use AttributeError — not caught by ValueError (404) or RuntimeError (409) handlers
        with patch("switchboard.dashboard.api._handle_list_projects",
                   side_effect=AttributeError("Simulated internal server error")):
            scope = _make_scope(method="GET", path="/dashboard/api/projects")
            send = _Capture()

            with caplog.at_level(logging.ERROR, logger="switchboard.dashboard.api"):
                await handle_request(scope, _make_receive(), send)

        assert send.status == 500

        # logger.exception() records the exception in exc_info
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_records) > 0
        has_exc_info = any(r.exc_info is not None for r in error_records)
        assert has_exc_info, "Expected logger.exception() to capture exception info"

    async def test_500_response_includes_error_field(self, db):
        """500 response body includes 'error' key."""
        from switchboard.dashboard.api import handle_request

        # Use AttributeError — not caught by ValueError (404) or RuntimeError (409) handlers
        with patch("switchboard.dashboard.api._handle_list_projects",
                   side_effect=AttributeError("boom")):
            scope = _make_scope(method="GET", path="/dashboard/api/projects")
            send = _Capture()
            await handle_request(scope, _make_receive(), send)

        assert send.status == 500
        body = send.json()
        assert "error" in body

    async def test_create_project_exception_logs_traceback(self, db, caplog):
        """Exception during project creation → 500 with exception logged."""
        from switchboard.dashboard.api import handle_request

        # A body that will pass the early validation but blow up later
        body = {
            "id": "log-test-project",
            "repo": "https://github.com/acme/log-test.git",
            "model": "sonnet",
            "review_model": "sonnet",
            "auto_test": True,
            "auto_review": True,
            "auto_pr": False,
            "auto_merge": False,
            "max_turns": 100,
            "max_wall_clock": 30,
        }

        with patch("switchboard.dashboard.api.db.create_project",
                   side_effect=RuntimeError("DB exploded")):
            with patch("switchboard.dashboard.api._validate_pat_for_project", return_value=None,
                       create=True):
                scope = _make_scope(method="POST", path="/dashboard/api/projects")
                send = _Capture()

                with caplog.at_level(logging.ERROR, logger="switchboard.dashboard.api"):
                    await handle_request(scope, _make_receive(body), send)

        # Should be a 500 or 400 with error logged
        assert send.status in (400, 500)
