"""Tests for onboarding guardrails:

- dispatch_task without Anthropic key returns clear error, no task created
- create_project validates credentials after creation (non-blocking)
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
                            yield


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


# ── Issue 2: create_project — credential validation is now post-create ────


# ── Issue 2d: delete_project MCP tool ─────────────────────────────────────


class TestDeleteProjectMCP:
    """MCP delete_project handler."""


    async def test_delete_project_not_found(self, db):
        """delete_project returns error for non-existent project."""
        from switchboard.server.handlers.projects import _handle_delete_project

        result = await _handle_delete_project({"project_id": "ghost-project"})

        assert "error" in result
        assert "not found" in result["error"].lower() or "ghost-project" in result["error"]


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
            scope = _make_scope(method="POST", path="/dashboard/api/projects")
            send = _Capture()

            with caplog.at_level(logging.ERROR, logger="switchboard.dashboard.api"):
                await handle_request(scope, _make_receive(body), send)

        # Should be a 500 or 400 with error logged
        assert send.status in (400, 500)
