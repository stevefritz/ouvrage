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
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search", query={})
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 400
        assert "q" in resp.json()["error"].lower()


class TestSearchApiEmbedFailure:
    """Validates graceful error when embeddings are unavailable."""

    async def test_embed_failure_returns_503(self, db):
        from switchboard.dashboard.api import handle_request

        error_result = {"error": "Failed to embed query — OPENAI_API_KEY must be set"}
        with patch(
            "switchboard.server.handlers.search._handle_search",
            new=AsyncMock(return_value=error_result),
        ):
            scope = _make_scope("/dashboard/api/search", query={"q": "authentication"})
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 503
        assert "error" in resp.json()


class TestSearchApiSuccess:
    """Validates successful search responses."""


    async def test_invalid_limit_defaults_to_10(self, db):
        from switchboard.dashboard.api import handle_request

        captured = {}

        async def mock_search(arguments):
            captured.update(arguments)
            return {"results": [], "total_candidates": 0}

        with patch("switchboard.server.handlers.search._handle_search", new=mock_search):
            scope = _make_scope("/dashboard/api/search", query={"q": "test", "limit": "abc"})
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        assert captured["limit"] == 10
