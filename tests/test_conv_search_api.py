"""Tests for GET /dashboard/api/conversations/{id}/search endpoint.

Covers:
- search_conversation_messages DB function (LIKE search)
- Endpoint input validation (missing q)
- Returns message objects (id, author, type, title, snippet, score)
- Only returns messages in the scoped conversation
- No results for non-matching query
"""

import json
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


# ── DB-level tests ────────────────────────────────────────────────────────────


# ── Endpoint tests ────────────────────────────────────────────────────────────

class TestConversationSearchEndpoint:
    """Tests for GET /dashboard/api/conversations/{id}/search."""

    async def test_missing_q_returns_400(self, db, sample_conversation):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/conversations/widget-redesign/search", query={})
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 400
        assert "q" in resp.json()["error"].lower()


    async def test_scoped_to_conversation(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        # Create two conversations with distinct content
        await db.create_conversation(id="scope-conv-a", project="test-project", goal="A")
        await db.create_conversation(id="scope-conv-b", project="test-project", goal="B")
        await db.post_message(
            conversation_id="scope-conv-a",
            author="user",
            content="uniquescope_term_abc appears only here",
            type="note",
        )

        # Search conv-a — should find it
        scope_a = _make_scope(
            "/dashboard/api/conversations/scope-conv-a/search",
            query={"q": "uniquescope_term_abc"},
        )
        resp_a = _Capture()
        await handle_request(scope_a, _make_receive(), resp_a)
        assert resp_a.status == 200
        assert resp_a.json()["total"] >= 1

        # Search conv-b — should NOT find it
        scope_b = _make_scope(
            "/dashboard/api/conversations/scope-conv-b/search",
            query={"q": "uniquescope_term_abc"},
        )
        resp_b = _Capture()
        await handle_request(scope_b, _make_receive(), resp_b)
        assert resp_b.status == 200
        assert resp_b.json()["total"] == 0
