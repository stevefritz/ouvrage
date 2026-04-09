"""Tests for dashboard settings API endpoints.

Covers all 8 settings endpoints:
- GET/PATCH /dashboard/api/settings/instance
- POST /dashboard/api/settings/instance/test-github
- POST /dashboard/api/settings/instance/regenerate-oauth-secret
- GET/PATCH /dashboard/api/settings/user
- POST /dashboard/api/settings/user/test-anthropic
- POST /dashboard/api/settings/user/change-password
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── ASGI test helpers ─────────────────────────────────────────────────────────

def _make_scope(path: str, method: str = "GET", role: str = "owner",
                user_id: int = 1, email: str = "owner@localhost") -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "session_user": {"id": user_id, "email": email, "name": "Owner", "role": role},
    }


def _make_receive(body=None):
    if body is None:
        raw = b""
    elif isinstance(body, dict):
        raw = json.dumps(body).encode()
    elif isinstance(body, bytes):
        raw = body
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


def _mock_httpx_response(status_code: int, json_data: dict):
    """Build a mock httpx response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    return mock_resp


def _patch_httpx(status_code: int, json_data: dict):
    """Context manager that patches httpx.AsyncClient to return a fixed response."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_mock_httpx_response(status_code, json_data))

    class _FakeCtx:
        async def __aenter__(self):
            return mock_client

        async def __aexit__(self, *args):
            return False

    return patch("switchboard.dashboard.api.httpx.AsyncClient", return_value=_FakeCtx())


# ── Instance settings ─────────────────────────────────────────────────────────

class TestGetInstanceSettings:

    @pytest.fixture(autouse=True)
    async def reset_oauth_keys(self, tmp_path, monkeypatch):
        import switchboard.auth.oauth as _oauth
        monkeypatch.setattr(_oauth, "OAUTH_RSA_KEY_PATH", str(tmp_path / "test_key.pem"))
        _oauth._rsa_private_key = None
        _oauth._rsa_public_jwk = None
        yield
        _oauth._rsa_private_key = None
        _oauth._rsa_public_jwk = None

    async def test_owner_gets_full_response(self, db):
        from switchboard.dashboard.api import handle_request

        await db.set_instance_github_pat("ghp_testtoken1234")
        scope = _make_scope("/dashboard/api/settings/instance")
        resp = _Capture()

        with _patch_httpx(200, {"login": "stevefritz"}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["instance"]["name"] == "Ouvrage"
        assert data["instance"]["slug"] == "default"
        assert data["github"]["connected"] is True
        assert data["github"]["username"] == "stevefritz"
        assert data["github"]["pat_last4"] == "1234"


class TestPatchInstanceSettings:


    async def test_member_gets_403(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance", method="PATCH", role="member")
        resp = _Capture()

        await handle_request(scope, _make_receive({"github_pat": "ghp_x"}), resp)

        assert resp.status == 403


class TestTestGithub:


    async def test_invalid_pat_returns_false(self, db):
        from switchboard.dashboard.api import handle_request

        await db.set_instance_github_pat("ghp_badtoken")
        scope = _make_scope("/dashboard/api/settings/instance/test-github", method="POST")
        resp = _Capture()

        with _patch_httpx(401, {"message": "Bad credentials"}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["valid"] is False
        assert "error" in data

    async def test_no_pat_configured_returns_false(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance/test-github", method="POST")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["valid"] is False

    async def test_member_gets_403(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance/test-github",
                            method="POST", role="member")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 403


class TestRegenerateOAuthSecret:

    @pytest.fixture(autouse=True)
    async def reset_oauth_keys(self, tmp_path, monkeypatch):
        import switchboard.auth.oauth as _oauth
        monkeypatch.setattr(_oauth, "OAUTH_RSA_KEY_PATH", str(tmp_path / "test_key.pem"))
        _oauth._rsa_private_key = None
        _oauth._rsa_public_jwk = None
        yield
        _oauth._rsa_private_key = None
        _oauth._rsa_public_jwk = None


    async def test_returns_404_when_oauth_client_not_seeded(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance/regenerate-oauth-secret",
                            method="POST")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 404


    async def test_member_gets_403(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance/regenerate-oauth-secret",
                            method="POST", role="member")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 403


# ── User settings ─────────────────────────────────────────────────────────────

class TestGetUserSettings:


    async def test_notifications_returned(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        prefs = {"task_completed": True, "task_failed": True}
        await db.update_user_credentials(owner["id"], notification_preferences=prefs)
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"],
                            email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert data["notifications"]["task_completed"] is True


class TestPatchUserSettings:

    async def test_update_name(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user", method="PATCH",
                            user_id=owner["id"], email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive({"name": "Stephen"}), resp)

        assert resp.status == 200
        updated = await db.get_user(owner["id"])
        assert updated["name"] == "Stephen"


    async def test_update_notification_prefs(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user", method="PATCH",
                            user_id=owner["id"], email="owner@localhost")
        resp = _Capture()
        prefs = {"task_completed": True, "task_failed": False}

        await handle_request(scope, _make_receive({"notification_preferences": prefs}), resp)

        assert resp.status == 200
        creds = await db.get_user_credentials(owner["id"])
        assert creds["notification_preferences"]["task_completed"] is True
        assert creds["notification_preferences"]["task_failed"] is False


class TestTestAnthropic:


    async def test_invalid_key_returns_false(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        await db.update_user_credentials(owner["id"], anthropic_api_key="sk-ant-bad")
        scope = _make_scope("/dashboard/api/settings/user/test-anthropic", method="POST",
                            user_id=owner["id"], email="owner@localhost")
        resp = _Capture()

        with _patch_httpx(401, {"error": {"type": "authentication_error"}}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["valid"] is False

    async def test_no_key_configured_returns_false(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user/test-anthropic", method="POST",
                            user_id=owner["id"], email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["valid"] is False
        assert "error" in data


class TestChangePassword:

    async def _create_user_with_password(self, db, email: str, password: str) -> dict:
        from argon2 import PasswordHasher
        ph = PasswordHasher()
        pw_hash = ph.hash(password)
        return await db.create_user(email=email, name="Test User",
                                    role="member", password_hash=pw_hash)


    async def test_missing_fields_returns_400(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user/change-password", method="POST",
                            user_id=owner["id"], email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive({"current_password": "only_one"}), resp)

        assert resp.status == 400

    async def test_no_password_hash_returns_400(self, db):
        """SSO/SaaS users with no local password get a clear 400 error, not a crash."""
        from switchboard.dashboard.api import handle_request

        # owner@localhost has no password_hash — simulates SSO user
        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user/change-password", method="POST",
                            user_id=owner["id"], email="owner@localhost")
        resp = _Capture()

        await handle_request(
            scope,
            _make_receive({"current_password": "anything", "new_password": "newpass123"}),
            resp,
        )

        assert resp.status == 400
        assert "password" in resp.json().get("error", "").lower()
