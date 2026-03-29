"""Tests for API token management endpoints.

Covers:
  GET  /dashboard/api/settings/tokens  — list tokens
  POST /dashboard/api/settings/tokens  — create token
  DELETE /dashboard/api/settings/tokens/{id} — revoke token
"""

import json

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


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestListTokens:

    async def test_list_tokens_empty(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/tokens", method="GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert "tokens" in data
        assert data["tokens"] == []

    async def test_list_tokens_unauthenticated(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/tokens", method="GET")
        del scope["session_user"]
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 401

    async def test_list_tokens_after_create(self, db):
        from switchboard.dashboard.api import handle_request

        # Create a token first
        create_scope = _make_scope("/dashboard/api/settings/tokens", method="POST")
        create_resp = _Capture()
        await handle_request(create_scope, _make_receive({"name": "My Token"}), create_resp)
        assert create_resp.status == 201

        # Now list
        scope = _make_scope("/dashboard/api/settings/tokens", method="GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert len(data["tokens"]) == 1
        token = data["tokens"][0]
        assert token["name"] == "My Token"
        assert "token_prefix" in token
        assert token["token_prefix"].startswith("sb_")
        assert "token_hash" not in token
        assert "token" not in token


class TestCreateToken:

    async def test_create_token_success(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/tokens", method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive({"name": "CI token"}), resp)

        assert resp.status == 201
        data = resp.json()
        assert data["name"] == "CI token"
        assert data["token"].startswith("sb_")
        assert len(data["token"]) == 67  # "sb_" + 64 hex chars
        assert "token_prefix" in data
        assert data["token_prefix"] == data["token"][:12]

    async def test_create_token_no_name(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/tokens", method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive({}), resp)

        assert resp.status == 201
        data = resp.json()
        assert data["name"] is None
        assert data["token"].startswith("sb_")

    async def test_create_token_empty_name_treated_as_null(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/tokens", method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive({"name": "  "}), resp)

        assert resp.status == 201
        data = resp.json()
        assert data["name"] is None

    async def test_create_token_unauthenticated(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/tokens", method="POST")
        del scope["session_user"]
        resp = _Capture()
        await handle_request(scope, _make_receive({"name": "bad"}), resp)

        assert resp.status == 401

    async def test_create_token_not_returned_in_list(self, db):
        """Raw token must NOT appear in the list endpoint — only prefix."""
        from switchboard.dashboard.api import handle_request

        create_scope = _make_scope("/dashboard/api/settings/tokens", method="POST")
        create_resp = _Capture()
        await handle_request(create_scope, _make_receive({"name": "test"}), create_resp)
        raw_token = create_resp.json()["token"]

        list_scope = _make_scope("/dashboard/api/settings/tokens", method="GET")
        list_resp = _Capture()
        await handle_request(list_scope, _make_receive(), list_resp)

        body_str = list_resp.body.decode()
        assert raw_token not in body_str


class TestRevokeToken:

    async def test_revoke_token_success(self, db):
        from switchboard.dashboard.api import handle_request

        # Create
        create_scope = _make_scope("/dashboard/api/settings/tokens", method="POST")
        create_resp = _Capture()
        await handle_request(create_scope, _make_receive({"name": "tmp"}), create_resp)
        token_id = create_resp.json()["id"]

        # Revoke
        scope = _make_scope(f"/dashboard/api/settings/tokens/{token_id}", method="DELETE")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        assert resp.json()["ok"] is True

    async def test_revoke_token_removes_from_list(self, db):
        from switchboard.dashboard.api import handle_request

        create_scope = _make_scope("/dashboard/api/settings/tokens", method="POST")
        create_resp = _Capture()
        await handle_request(create_scope, _make_receive({"name": "del-me"}), create_resp)
        token_id = create_resp.json()["id"]

        # Revoke
        del_scope = _make_scope(f"/dashboard/api/settings/tokens/{token_id}", method="DELETE")
        del_resp = _Capture()
        await handle_request(del_scope, _make_receive(), del_resp)
        assert del_resp.status == 200

        # List — should be empty now
        list_scope = _make_scope("/dashboard/api/settings/tokens", method="GET")
        list_resp = _Capture()
        await handle_request(list_scope, _make_receive(), list_resp)
        assert list_resp.json()["tokens"] == []

    async def test_revoke_token_not_found(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/tokens/9999", method="DELETE")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 404

    async def test_revoke_token_invalid_id(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/tokens/notanumber", method="DELETE")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 400

    async def test_revoke_token_unauthenticated(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/tokens/1", method="DELETE")
        del scope["session_user"]
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 401

    async def test_revoke_other_users_token_not_found(self, db):
        """Users can only revoke their own tokens."""
        from switchboard.dashboard.api import handle_request
        import switchboard.db as sw_db

        # Create a second user
        second_user = await sw_db.create_user(
            email="other@example.com", name="Other", role="member"
        )
        second_user_id = second_user["id"]

        # Create token for second user directly via DB
        result = await sw_db.create_api_token(second_user_id, name="other-token")
        token_id = result["id"]

        # Try to revoke as user 1 — should 404
        scope = _make_scope(f"/dashboard/api/settings/tokens/{token_id}", method="DELETE", user_id=1)
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 404
