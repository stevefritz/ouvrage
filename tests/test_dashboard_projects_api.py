"""Tests for POST /dashboard/api/projects endpoint."""

import json
from unittest.mock import patch

import pytest


# ── ASGI test helpers ─────────────────────────────────────────────────────────

def _make_scope(path: str, method: str = "GET", user_id: int = 1) -> dict:
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


def _valid_payload(**overrides):
    base = {
        "id": "test-proj",
        "repo": "https://github.com/org/repo.git",
        "default_branch": "main",
        "model": "claude-sonnet-4-6",
        "review_model": "claude-opus-4-6",
        "auto_test": True,
        "auto_review": True,
        "auto_pr": False,
        "auto_merge": False,
        "max_turns": 200,
        "max_wall_clock": 60,
    }
    base.update(overrides)
    return base


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPostProjects:

    async def test_create_project_success(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()

        with patch("switchboard.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(_valid_payload()), resp)

        assert resp.status == 201
        data = resp.json()
        assert data["id"] == "test-proj"
        assert data["repo"] == "https://github.com/org/repo.git"
        assert data["model"] == "claude-sonnet-4-6"
        assert data["review_model"] == "claude-opus-4-6"
        assert data["max_turns"] == 200
        assert data["max_wall_clock"] == 60

    async def test_create_project_persisted_in_db(self, db):
        from switchboard.dashboard.api import handle_request
        import switchboard.db as sw_db

        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()

        with patch("switchboard.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(_valid_payload()), resp)

        assert resp.status == 201
        project = await sw_db.get_project("test-proj")
        assert project is not None
        assert project["id"] == "test-proj"

    async def test_create_project_missing_id(self, db):
        from switchboard.dashboard.api import handle_request

        payload = _valid_payload()
        del payload["id"]
        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()

        with patch("switchboard.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 400
        assert "id is required" in resp.json()["error"]

    async def test_create_project_missing_repo(self, db):
        from switchboard.dashboard.api import handle_request

        payload = _valid_payload()
        del payload["repo"]
        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()

        with patch("switchboard.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 400
        assert "repo is required" in resp.json()["error"]

    async def test_create_project_invalid_id_format(self, db):
        from switchboard.dashboard.api import handle_request

        payload = _valid_payload(id="My Project!")
        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()

        with patch("switchboard.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 400
        data = resp.json()
        assert "id must start with" in data["error"]

    async def test_create_project_missing_required_config(self, db):
        from switchboard.dashboard.api import handle_request

        payload = _valid_payload()
        del payload["model"]
        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()

        with patch("switchboard.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 400
        assert "model" in resp.json()["error"]

    async def test_create_project_with_optional_fields(self, db):
        from switchboard.dashboard.api import handle_request

        payload = _valid_payload(
            id="opt-proj",
            test_command="pytest -v",
            setup_command="pip install -r requirements.txt",
            teardown_command="make clean",
            review_ignore_patterns=["*.lock", "vendor/"],
            env_overrides={"KEY": "value"},
        )
        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()

        with patch("switchboard.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 201
        data = resp.json()
        assert data["test_command"] == "pytest -v"
        assert data["setup_command"] == "pip install -r requirements.txt"
        assert data["teardown_command"] == "make clean"
        assert data["review_ignore_patterns"] == ["*.lock", "vendor/"]
        assert data["env_overrides"] == {"KEY": "value"}

    async def test_create_project_invalid_json(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()

        with patch("switchboard.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(b"not json"), resp)

        assert resp.status == 400
        assert "Invalid JSON" in resp.json()["error"]

    async def test_create_project_working_dir_collision(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp1 = _Capture()

        with patch("switchboard.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(_valid_payload()), resp1)
        assert resp1.status == 201

        # Same repo → same working_dir → collision
        resp2 = _Capture()
        payload2 = _valid_payload(id="other-proj")  # different id, same repo
        with patch("switchboard.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(payload2), resp2)

        assert resp2.status == 400
        assert "already belongs to project" in resp2.json()["error"]
