"""Tests for POST /dashboard/api/tasks — create task via dashboard form."""

import json
from unittest.mock import AsyncMock, patch

import pytest


# ── ASGI test helpers ─────────────────────────────────────────────────────────

def _make_scope(method="POST", path="/dashboard/api/tasks"):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "session_user": {"id": 1, "email": "owner@localhost", "name": "Owner", "role": "owner"},
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


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestCreateTaskEndpoint:
    """POST /dashboard/api/tasks"""

    async def test_create_held_task_success(self, db, sample_project, mock_git):
        """Happy path: creates a held task, returns 201 with task_id."""
        from ouvrage.dashboard.api import handle_request

        body = {
            "project_id": "test-project",
            "id": "test-project/new-task-from-form",
            "goal": "Build something awesome",
            "held": True,
        }
        scope = _make_scope()
        receive = _make_receive(body)
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 201
        resp = send.json()
        assert resp["task_id"] == "test-project/new-task-from-form"
        assert resp["project_id"] == "test-project"

        # Verify task is in DB and held
        task = await db.get_task("test-project/new-task-from-form")
        assert task is not None
        assert task["held"] == 1
        assert task["status"] == "ready"
        assert task["goal"] == "Build something awesome"

    async def test_create_task_with_spec_and_checklist(self, db, sample_project, mock_git):
        """Task can be created with spec and checklist items."""
        from ouvrage.dashboard.api import handle_request

        body = {
            "project_id": "test-project",
            "id": "test-project/task-with-spec",
            "goal": "Task with full spec",
            "spec": "## Overview\nDo the thing.",
            "checklist": ["Step 1", "Step 2", "Step 3"],
            "held": True,
        }
        scope = _make_scope()
        receive = _make_receive(body)
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 201
        task = await db.get_task("test-project/task-with-spec")
        assert task is not None
        assert task["goal"] == "Task with full spec"
        # Spec is stored as a pinned message
        pinned = await db.get_task_pinned("test-project/task-with-spec")
        assert pinned is not None
        assert "Overview" in pinned["content"]

    async def test_missing_project_id_returns_400(self, db, sample_project):
        """Missing project_id → 400 error."""
        from ouvrage.dashboard.api import handle_request

        body = {"id": "test-project/task-x", "goal": "Do something"}
        scope = _make_scope()
        receive = _make_receive(body)
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 400
        assert "project_id" in send.json()["error"]

    async def test_missing_goal_returns_400(self, db, sample_project):
        """Missing goal → 400 error."""
        from ouvrage.dashboard.api import handle_request

        body = {"project_id": "test-project", "id": "test-project/task-x"}
        scope = _make_scope()
        receive = _make_receive(body)
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 400
        assert "goal" in send.json()["error"]

    async def test_missing_task_id_returns_400(self, db, sample_project):
        """Missing task id → 400 error."""
        from ouvrage.dashboard.api import handle_request

        body = {"project_id": "test-project", "goal": "Do something"}
        scope = _make_scope()
        receive = _make_receive(body)
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 400
        assert "id" in send.json()["error"]

    async def test_duplicate_task_id_returns_409(self, db, sample_project, sample_task, mock_git):
        """Creating a task with an ID that already exists → 409."""
        from ouvrage.dashboard.api import handle_request

        body = {
            "project_id": "test-project",
            "id": "test-project/implement-feature",  # already exists (sample_task)
            "goal": "Duplicate",
            "held": True,
        }
        scope = _make_scope()
        receive = _make_receive(body)
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 409
        assert "already exists" in send.json()["error"]

    async def test_unknown_project_returns_error(self, db):
        """Project doesn't exist → error from dispatch_task."""
        from ouvrage.dashboard.api import handle_request

        body = {
            "project_id": "nonexistent-project",
            "id": "nonexistent-project/task-x",
            "goal": "Do something",
            "held": True,
        }
        scope = _make_scope()
        receive = _make_receive(body)
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 400
        assert "not found" in send.json()["error"].lower()

    async def test_empty_body_returns_400(self, db, sample_project):
        """Empty request body → 400."""
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope()
        receive = _make_receive(b"")
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 400

    async def test_get_tasks_still_works(self, db, sample_project):
        """GET /dashboard/api/tasks is not broken by the new POST route."""
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope(method="GET")
        receive = _make_receive()
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 200
        result = send.json()
        assert isinstance(result, list)
