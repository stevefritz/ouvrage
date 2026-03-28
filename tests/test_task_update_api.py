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

    async def test_update_multiple_fields(self, db, sample_task):
        """Update several metadata fields at once."""
        from switchboard.dashboard.api import handle_request

        task_id = sample_task["id"]
        payload = {
            "base_branch": "develop",
            "branch_target": "main",
            "model": "opus",
            "max_turns": 50,
        }
        scope = _make_scope(path=_task_path(task_id))
        receive = _make_receive(payload)
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 200
        resp = send.json()
        assert resp["base_branch"] == "develop"
        assert resp["branch_target"] == "main"
        assert resp["model"] == "opus"
        assert resp["max_turns"] == 50

    async def test_update_tags(self, db, sample_task):
        """Tags are replaced (not appended)."""
        from switchboard.dashboard.api import handle_request

        task_id = sample_task["id"]
        scope = _make_scope(path=_task_path(task_id))
        receive = _make_receive({"tags": ["alpha", "beta"]})
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 200
        resp = send.json()
        assert sorted(resp.get("tags", [])) == ["alpha", "beta"]

    async def test_update_boolean_fields(self, db, sample_task):
        """auto_* boolean fields can be toggled."""
        from switchboard.dashboard.api import handle_request

        task_id = sample_task["id"]
        scope = _make_scope(path=_task_path(task_id))
        receive = _make_receive({"auto_test": False, "auto_review": True})
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 200
        updated = await db.get_task(task_id)
        assert updated["auto_test"] == 0
        assert updated["auto_review"] == 1

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

    async def test_update_conversation_id(self, db, sample_task):
        """conversation_id field can be set."""
        from switchboard.dashboard.api import handle_request

        task_id = sample_task["id"]
        scope = _make_scope(path=_task_path(task_id))
        receive = _make_receive({"conversation_id": "my-convo"})
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 200
        resp = send.json()
        assert resp["conversation_id"] == "my-convo"

    async def test_update_component_id_cleared(self, db, sample_task):
        """component_id can be cleared (set to null)."""
        from switchboard.dashboard.api import handle_request

        task_id = sample_task["id"]
        scope = _make_scope(path=_task_path(task_id))
        receive = _make_receive({"component_id": None})
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 200
        updated = await db.get_task(task_id)
        assert updated["component_id"] is None

    async def test_update_returns_updated_task(self, db, sample_task):
        """Response body is the updated task record."""
        from switchboard.dashboard.api import handle_request

        task_id = sample_task["id"]
        scope = _make_scope(path=_task_path(task_id))
        receive = _make_receive({"claude_chat_url": "https://claude.ai/chat/abc123"})
        send = _Capture()

        await handle_request(scope, receive, send)

        assert send.status == 200
        resp = send.json()
        assert resp["id"] == task_id
        assert resp["claude_chat_url"] == "https://claude.ai/chat/abc123"
