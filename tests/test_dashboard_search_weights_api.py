"""Tests for search weight CRUD endpoints in the dashboard API.

POST   /dashboard/api/search/weight
DELETE /dashboard/api/search/weight
GET    /dashboard/api/search/weights
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


def _make_scope_no_auth(path: str, method: str = "GET") -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
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


# ── POST /dashboard/api/search/weight ─────────────────────────────────────────

class TestPostSearchWeight:
    """POST creates or updates a weight row."""

    async def test_creates_weight(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search/weight", method="POST")
        body = {"entity_type": "task", "entity_id": "proj/task-1", "weight": 1.5}
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["entity_type"] == "task"
        assert data["entity_id"] == "proj/task-1"
        assert data["weight"] == 1.5

    async def test_updates_existing_weight(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search/weight", method="POST")

        # Create
        body1 = {"entity_type": "task", "entity_id": "proj/task-1", "weight": 1.0}
        resp1 = _Capture()
        await handle_request(scope, _make_receive(body1), resp1)
        assert resp1.status == 200

        # Update
        body2 = {"entity_type": "task", "entity_id": "proj/task-1", "weight": 2.0, "reason": "updated"}
        resp2 = _Capture()
        await handle_request(scope, _make_receive(body2), resp2)
        assert resp2.status == 200
        data = resp2.json()
        assert data["weight"] == 2.0
        assert data["reason"] == "updated"

    async def test_reason_is_optional(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search/weight", method="POST")
        body = {"entity_type": "message", "entity_id": "msg-42", "weight": 0.5}
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 200
        assert resp.json()["reason"] is None

    async def test_missing_entity_type_returns_400(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search/weight", method="POST")
        body = {"entity_id": "proj/task-1", "weight": 1.0}
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 400
        assert "entity_type" in resp.json()["error"]

    async def test_missing_entity_id_returns_400(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search/weight", method="POST")
        body = {"entity_type": "task", "weight": 1.0}
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 400
        assert "entity_id" in resp.json()["error"]

    async def test_missing_weight_returns_400(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search/weight", method="POST")
        body = {"entity_type": "task", "entity_id": "proj/task-1"}
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 400
        assert "weight" in resp.json()["error"]

    async def test_invalid_entity_type_returns_400(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search/weight", method="POST")
        body = {"entity_type": "badtype", "entity_id": "proj/task-1", "weight": 1.0}
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 400
        assert "entity_type" in resp.json()["error"].lower() or "invalid" in resp.json()["error"].lower()

    async def test_out_of_range_weight_returns_400(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search/weight", method="POST")
        body = {"entity_type": "task", "entity_id": "proj/task-1", "weight": 9.9}
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 400

    async def test_requires_auth(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope_no_auth("/dashboard/api/search/weight", method="POST")
        body = {"entity_type": "task", "entity_id": "proj/task-1", "weight": 1.0}
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 401


# ── DELETE /dashboard/api/search/weight ───────────────────────────────────────

class TestDeleteSearchWeight:
    """DELETE removes a weight row; idempotent (204 on no-op)."""

    async def test_removes_existing_weight(self, db):
        from ouvrage.dashboard.api import handle_request

        # Create first
        post_scope = _make_scope("/dashboard/api/search/weight", method="POST")
        body = {"entity_type": "task", "entity_id": "proj/task-1", "weight": 1.5}
        await handle_request(post_scope, _make_receive(body), _Capture())

        # Delete
        del_scope = _make_scope("/dashboard/api/search/weight", method="DELETE")
        del_body = {"entity_type": "task", "entity_id": "proj/task-1"}
        resp = _Capture()
        await handle_request(del_scope, _make_receive(del_body), resp)

        assert resp.status == 204
        assert resp.body == b""

    async def test_delete_nonexistent_is_204(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search/weight", method="DELETE")
        body = {"entity_type": "task", "entity_id": "nonexistent-task"}
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 204

    async def test_missing_entity_type_returns_400(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search/weight", method="DELETE")
        body = {"entity_id": "proj/task-1"}
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 400
        assert "entity_type" in resp.json()["error"]

    async def test_missing_entity_id_returns_400(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search/weight", method="DELETE")
        body = {"entity_type": "task"}
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 400
        assert "entity_id" in resp.json()["error"]

    async def test_requires_auth(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope_no_auth("/dashboard/api/search/weight", method="DELETE")
        body = {"entity_type": "task", "entity_id": "proj/task-1"}
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 401


# ── GET /dashboard/api/search/weights ─────────────────────────────────────────

class TestGetSearchWeights:
    """GET lists all weights, optionally filtered by entity_type."""

    async def test_returns_empty_list_initially(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/search/weights")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        assert resp.json() == []

    async def test_lists_all_weights(self, db):
        from ouvrage.dashboard.api import handle_request

        # Seed two weights
        post_scope = _make_scope("/dashboard/api/search/weight", method="POST")
        await handle_request(
            post_scope,
            _make_receive({"entity_type": "task", "entity_id": "t1", "weight": 1.0}),
            _Capture(),
        )
        await handle_request(
            post_scope,
            _make_receive({"entity_type": "message", "entity_id": "m1", "weight": 0.5}),
            _Capture(),
        )

        get_scope = _make_scope("/dashboard/api/search/weights")
        resp = _Capture()
        await handle_request(get_scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert len(data) == 2
        entity_types = {r["entity_type"] for r in data}
        assert entity_types == {"task", "message"}

    async def test_filters_by_entity_type(self, db):
        from ouvrage.dashboard.api import handle_request

        # Seed two tasks and one message
        post_scope = _make_scope("/dashboard/api/search/weight", method="POST")
        for eid in ["t1", "t2"]:
            await handle_request(
                post_scope,
                _make_receive({"entity_type": "task", "entity_id": eid, "weight": 1.0}),
                _Capture(),
            )
        await handle_request(
            post_scope,
            _make_receive({"entity_type": "message", "entity_id": "m1", "weight": 0.5}),
            _Capture(),
        )

        get_scope = _make_scope("/dashboard/api/search/weights", query={"entity_type": "task"})
        resp = _Capture()
        await handle_request(get_scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert len(data) == 2
        assert all(r["entity_type"] == "task" for r in data)

    async def test_requires_auth(self, db):
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope_no_auth("/dashboard/api/search/weights")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 401
