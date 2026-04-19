"""Tests for GET /dashboard/api/search endpoint."""

import json
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import pytest


# ── ASGI test helpers ─────────────────────────────────────────────────────────

def _make_scope(path: str, method: str = "GET", query: dict = None, user_id: int = 1) -> dict:
    qs = urlencode(query or {}).encode()
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": qs,
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


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSearchApiMissingQuery:
    """Validates input validation — missing q param."""

    async def test_missing_q_returns_400(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search", query={})
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 400
        assert "q" in resp.json()["error"].lower()

    async def test_empty_q_returns_400(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search", query={"q": ""})
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 400


class TestSearchApiEmbedFailure:
    """Validates graceful error when embeddings are unavailable."""

    async def test_embed_failure_returns_503(self, db):
        from ouvrage.dashboard.api import handle_request

        error_result = {"error": "Failed to embed query — OPENAI_API_KEY must be set"}
        with patch(
            "ouvrage.server.handlers.search._handle_search",
            new=AsyncMock(return_value=error_result),
        ):
            scope = _make_scope("/dashboard/api/search", query={"q": "authentication"})
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 503
        assert "error" in resp.json()


class TestSearchApiSuccess:
    """Validates successful search responses."""

    async def test_returns_results_array(self, db):
        from ouvrage.dashboard.api import handle_request

        mock_results = [
            {
                "id": "my-proj/task-1",
                "goal": "Implement auth module",
                "status": "working",
                "last_activity": "2026-01-01T00:00:00Z",
            }
        ]
        with patch(
            "ouvrage.server.handlers.search._handle_search",
            new=AsyncMock(return_value={"results": mock_results, "total_candidates": 1}),
        ):
            scope = _make_scope("/dashboard/api/search", query={"q": "auth"})
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert "results" in data
        assert len(data["results"]) == 1
        assert data["results"][0]["id"] == "my-proj/task-1"

    async def test_project_id_passed_through(self, db):
        from ouvrage.dashboard.api import handle_request

        captured = {}

        async def mock_search(arguments):
            captured.update(arguments)
            return {"results": [], "total_candidates": 0}

        with patch("ouvrage.server.handlers.search._handle_search", new=mock_search):
            scope = _make_scope(
                "/dashboard/api/search",
                query={"q": "auth", "project_id": "my-project"},
            )
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        assert captured["project_id"] == "my-project"
        assert captured["query"] == "auth"

    async def test_default_limit_is_10(self, db):
        from ouvrage.dashboard.api import handle_request

        captured = {}

        async def mock_search(arguments):
            captured.update(arguments)
            return {"results": [], "total_candidates": 0}

        with patch("ouvrage.server.handlers.search._handle_search", new=mock_search):
            scope = _make_scope("/dashboard/api/search", query={"q": "test"})
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        assert captured["limit"] == 10

    async def test_custom_limit_passed_through(self, db):
        from ouvrage.dashboard.api import handle_request

        captured = {}

        async def mock_search(arguments):
            captured.update(arguments)
            return {"results": [], "total_candidates": 0}

        with patch("ouvrage.server.handlers.search._handle_search", new=mock_search):
            scope = _make_scope("/dashboard/api/search", query={"q": "test", "limit": "5"})
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        assert captured["limit"] == 5

    async def test_no_project_id_passes_none(self, db):
        from ouvrage.dashboard.api import handle_request

        captured = {}

        async def mock_search(arguments):
            captured.update(arguments)
            return {"results": [], "total_candidates": 0}

        with patch("ouvrage.server.handlers.search._handle_search", new=mock_search):
            scope = _make_scope("/dashboard/api/search", query={"q": "auth"})
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        assert captured["project_id"] is None

    async def test_returns_task_objects(self, db):
        """Search API passes through task objects returned by the handler."""
        from ouvrage.dashboard.api import handle_request

        mock_results = [
            {"id": "proj/t1", "goal": "Task goal", "status": "working", "last_activity": "2026-01-01T00:00:00Z"},
            {"id": "proj/t2", "goal": "Another task", "status": "completed", "last_activity": "2026-01-02T00:00:00Z"},
        ]
        with patch(
            "ouvrage.server.handlers.search._handle_search",
            new=AsyncMock(return_value={"results": mock_results, "total_candidates": 2}),
        ):
            scope = _make_scope("/dashboard/api/search", query={"q": "test"})
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert len(data["results"]) == 2
        assert data["results"][0]["id"] == "proj/t1"
        assert data["results"][0]["goal"] == "Task goal"

    async def test_invalid_limit_defaults_to_10(self, db):
        from ouvrage.dashboard.api import handle_request

        captured = {}

        async def mock_search(arguments):
            captured.update(arguments)
            return {"results": [], "total_candidates": 0}

        with patch("ouvrage.server.handlers.search._handle_search", new=mock_search):
            scope = _make_scope("/dashboard/api/search", query={"q": "test", "limit": "abc"})
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        assert captured["limit"] == 10
