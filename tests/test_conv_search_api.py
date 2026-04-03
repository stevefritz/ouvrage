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

class TestSearchConversationMessages:
    """Unit tests for db.search_conversation_messages."""

    async def test_returns_matching_messages(self, db, sample_conversation):
        results = await db.search_conversation_messages("widget-redesign", "redesign")
        assert len(results) >= 1
        assert all(isinstance(r["id"], int) for r in results)
        assert all("snippet" in r for r in results)
        assert all("score" in r for r in results)
        assert all(r["score"] == 1.0 for r in results)

    async def test_snippet_contains_query_term(self, db, sample_conversation):
        results = await db.search_conversation_messages("widget-redesign", "sorting")
        assert len(results) >= 1
        assert any("sorting" in r["snippet"].lower() for r in results)

    async def test_no_match_returns_empty(self, db, sample_conversation):
        results = await db.search_conversation_messages("widget-redesign", "zzz_no_match_xyz")
        assert results == []

    async def test_scoped_to_conversation(self, db, sample_project):
        # Create two conversations; only the target one has a matching message
        await db.create_conversation(id="conv-a", project="test-project", goal="Conv A")
        await db.create_conversation(id="conv-b", project="test-project", goal="Conv B")
        await db.post_message(
            conversation_id="conv-a",
            author="user",
            content="uniqueterm_alpha is here",
            type="note",
        )
        await db.post_message(
            conversation_id="conv-b",
            author="user",
            content="this message does not have the term",
            type="note",
        )

        results = await db.search_conversation_messages("conv-a", "uniqueterm_alpha")
        assert len(results) == 1

        # conv-b has no match
        results_b = await db.search_conversation_messages("conv-b", "uniqueterm_alpha")
        assert len(results_b) == 0

    async def test_result_schema(self, db, sample_conversation):
        results = await db.search_conversation_messages("widget-redesign", "widget")
        assert len(results) >= 1
        r = results[0]
        assert "id" in r
        assert "author" in r
        assert "type" in r
        assert "snippet" in r
        assert "score" in r
        assert "created_at" in r
        # content should not be in result (replaced by snippet)
        assert "content" not in r

    async def test_snippet_length_capped(self, db, sample_project):
        # Create a message with very long content
        long_content = "start " + ("filler " * 100) + "KEYWORD " + ("more " * 100) + "end"
        await db.create_conversation(id="long-conv", project="test-project", goal="Long")
        await db.post_message(
            conversation_id="long-conv",
            author="bot",
            content=long_content,
            type="note",
        )
        results = await db.search_conversation_messages("long-conv", "KEYWORD")
        assert len(results) == 1
        # Snippet should be much shorter than the full content
        assert len(results[0]["snippet"]) < len(long_content)


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

    async def test_empty_q_returns_400(self, db, sample_conversation):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/conversations/widget-redesign/search", query={"q": ""})
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 400

    async def test_returns_matching_messages(self, db, sample_conversation):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/conversations/widget-redesign/search", query={"q": "redesign"})
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert "results" in data
        assert "total" in data
        assert data["total"] >= 1
        assert len(data["results"]) == data["total"]

    async def test_result_contains_message_fields(self, db, sample_conversation):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/conversations/widget-redesign/search", query={"q": "widget"})
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["total"] >= 1
        r = data["results"][0]
        assert "id" in r
        assert "author" in r
        assert "type" in r
        assert "snippet" in r
        assert "score" in r
        # Must NOT be task objects
        assert "task_id" not in r or r.get("task_id") is None  # no task grouping

    async def test_no_match_returns_empty_results(self, db, sample_conversation):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope(
            "/dashboard/api/conversations/widget-redesign/search",
            query={"q": "zzz_no_match_xyz"},
        )
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["results"] == []

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
