"""Tests for PATCH /dashboard/api/tasks/{task_id} — update task metadata."""

import json

import pytest


# ── ASGI test helpers ──────────────────────────────────────────────────────────

def _make_scope(method="PATCH", path="/dashboard/api/tasks/test-project/implement-feature"):
    """Build a minimal ASGI scope. Path uses decoded slashes (as real ASGI does)."""
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


def _task_path(task_id):
    """Build a PATCH path for a task ID (decoded, as ASGI delivers it)."""
    return f"/dashboard/api/tasks/{task_id}"


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestUpdateTaskEndpoint:
    """PATCH /dashboard/api/tasks/{task_id}"""

    async def test_update_single_field(self, db, sample_task):
        """Happy path: update a single mutable field."""
        from switchboard.dashboard.api import handle_request

        task_id = sample_task["id"]
        scope = _make_scope(path=_task_path(task_id))
        receive = _make_receive({"jira_ticket": "PROJ-42"})
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 200
        resp = send.json()
        assert resp["jira_ticket"] == "PROJ-42"

        # Confirm persisted
        updated = await db.get_task(task_id)
        assert updated["jira_ticket"] == "PROJ-42"


    async def test_update_unknown_task_returns_404(self, db, sample_project):
        """PATCH on non-existent task returns 404."""
        from switchboard.dashboard.api import handle_request

        scope = _make_scope(path="/dashboard/api/tasks/test-project/does-not-exist")
        receive = _make_receive({"jira_ticket": "PROJ-99"})
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 404
        resp = send.json()
        assert "error" in resp

    async def test_update_empty_body_returns_400(self, db, sample_task):
        """PATCH with empty body returns 400."""
        from switchboard.dashboard.api import handle_request

        task_id = sample_task["id"]
        scope = _make_scope(path=_task_path(task_id))
        receive = _make_receive({})
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 400
        resp = send.json()
        assert "error" in resp


