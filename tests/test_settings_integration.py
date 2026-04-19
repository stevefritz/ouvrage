"""Integration tests for settings API — multi-step workflows and response contracts.

These tests verify the JSON response shapes that the Settings UI depends on,
and test multi-step flows like save-then-read, update-then-verify.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── ASGI test helpers (duplicated from test_settings_api.py for isolation) ────

def _make_scope(path, method="GET", role="owner", user_id=1, email="owner@localhost"):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "session_user": {"id": user_id, "email": email, "name": "Owner", "role": role},
    }


def _make_receive(body=None):
    raw = b""
    if isinstance(body, dict):
        raw = json.dumps(body).encode()
    elif isinstance(body, bytes):
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


def _mock_httpx_response(status_code, json_data):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    return mock_resp


def _patch_httpx(status_code, json_data):
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_mock_httpx_response(status_code, json_data))

    class _FakeCtx:
        async def __aenter__(self):
            return mock_client
        async def __aexit__(self, *args):
            return False

    return patch("switchboard.dashboard.api.httpx.AsyncClient", return_value=_FakeCtx())


# ── Response shape contract tests ─────────────────────────────────────────────

class TestUserSettingsResponseContract:
    """Verify the exact JSON structure the Settings UI expects from GET /settings/user."""

    async def test_response_has_required_top_level_keys(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"])
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert "profile" in data, "Missing 'profile' key"
        assert "anthropic" in data, "Missing 'anthropic' key"
        assert "notifications" in data, "Missing 'notifications' key"

    async def test_profile_has_required_fields(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"])
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        profile = resp.json()["profile"]
        assert "name" in profile
        assert "email" in profile
        assert "timezone" in profile
        assert "role" in profile

    async def test_anthropic_has_required_fields(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"])
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        anthropic = resp.json()["anthropic"]
        assert "configured" in anthropic
        assert "key_last4" in anthropic
        assert isinstance(anthropic["configured"], bool)


class TestInstanceSettingsResponseContract:
    """Verify the exact JSON structure the Settings UI expects from GET /settings/instance."""

    async def test_response_has_required_top_level_keys(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance")
        resp = _Capture()

        with _patch_httpx(200, {}):
            await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert "instance" in data, "Missing 'instance' key"
        assert "github" in data, "Missing 'github' key"
        assert "oauth" in data, "Missing 'oauth' key"

    async def test_github_has_connected_field(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        github = resp.json()["github"]
        assert "connected" in github
        assert isinstance(github["connected"], bool)

    async def test_instance_has_required_fields(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance")
        resp = _Capture()

        with _patch_httpx(200, {}):
            await handle_request(scope, _make_receive(), resp)

        instance = resp.json()["instance"]
        assert "name" in instance
        assert "slug" in instance


# ── Multi-step workflow tests ─────────────────────────────────────────────────

class TestSaveThenReadPATWorkflow:
    """Test: save a GitHub PAT → read instance settings → verify it shows up."""

    async def test_save_pat_then_read_shows_connected(self, db):
        from switchboard.dashboard.api import handle_request

        # Step 1: Save a PAT
        scope = _make_scope("/dashboard/api/settings/instance", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"github_pat": "ghp_workflow1234"}), resp)
        assert resp.status == 200

        # Step 2: Read instance settings — should show the PAT last4
        scope = _make_scope("/dashboard/api/settings/instance")
        resp = _Capture()
        with _patch_httpx(200, {"login": "testbot"}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["github"]["connected"] is True
        assert data["github"]["pat_last4"] == "1234"
        assert data["github"]["username"] == "testbot"

    async def test_save_pat_then_test_connection(self, db):
        from switchboard.dashboard.api import handle_request

        # Step 1: Save
        scope = _make_scope("/dashboard/api/settings/instance", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"github_pat": "ghp_testme5678"}), resp)
        assert resp.status == 200

        # Step 2: Test connection
        scope = _make_scope("/dashboard/api/settings/instance/test-github", method="POST")
        resp = _Capture()
        with _patch_httpx(200, {"login": "myuser"}):
            await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert data["valid"] is True
        assert data["username"] == "myuser"


class TestSaveThenReadAnthropicWorkflow:
    """Test: save Anthropic key → read user settings → verify it shows up."""

    async def test_save_key_then_read_shows_configured(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")

        # Step 1: Save key
        scope = _make_scope("/dashboard/api/settings/user", method="PATCH",
                            user_id=owner["id"])
        resp = _Capture()
        await handle_request(scope, _make_receive({"anthropic_api_key": "sk-ant-mykey9999"}), resp)
        assert resp.status == 200

        # Step 2: Read user settings
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"])
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert data["anthropic"]["configured"] is True
        assert data["anthropic"]["key_last4"] == "9999"

    async def test_save_key_then_test_connection(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")

        # Step 1: Save
        scope = _make_scope("/dashboard/api/settings/user", method="PATCH",
                            user_id=owner["id"])
        resp = _Capture()
        await handle_request(scope, _make_receive({"anthropic_api_key": "sk-ant-valid"}), resp)
        assert resp.status == 200

        # Step 2: Test
        scope = _make_scope("/dashboard/api/settings/user/test-anthropic", method="POST",
                            user_id=owner["id"])
        resp = _Capture()
        with _patch_httpx(200, {"data": []}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.json()["valid"] is True


class TestUpdateProfileWorkflow:
    """Test: update profile fields → read back → verify changes persisted."""

    async def test_update_all_profile_fields_then_read(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")

        # Step 1: Update
        scope = _make_scope("/dashboard/api/settings/user", method="PATCH",
                            user_id=owner["id"])
        resp = _Capture()
        await handle_request(scope, _make_receive({
            "name": "Test User",
            "timezone": "America/New_York",
        }), resp)
        assert resp.status == 200

        # Step 2: Read back
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"])
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        profile = resp.json()["profile"]
        assert profile["name"] == "Test User"
        assert profile["timezone"] == "America/New_York"


class TestChangePasswordWorkflow:
    """Test: change password → verify old password no longer works → new one does."""

    async def _create_user_with_password(self, db, email, password):
        from argon2 import PasswordHasher
        ph = PasswordHasher()
        return await db.create_user(email=email, name="Test", role="member",
                                    password_hash=ph.hash(password))

    async def test_change_password_then_old_fails(self, db):
        from switchboard.dashboard.api import handle_request

        user = await self._create_user_with_password(db, "flow@test.com", "pass1")

        # Step 1: Change password
        scope = _make_scope("/dashboard/api/settings/user/change-password", method="POST",
                            user_id=user["id"], email="flow@test.com", role="member")
        resp = _Capture()
        await handle_request(scope, _make_receive({
            "current_password": "pass1",
            "new_password": "pass2new",
        }), resp)
        assert resp.status == 200

        # Step 2: Old password should fail
        scope = _make_scope("/dashboard/api/settings/user/change-password", method="POST",
                            user_id=user["id"], email="flow@test.com", role="member")
        resp = _Capture()
        await handle_request(scope, _make_receive({
            "current_password": "pass1",
            "new_password": "pass3",
        }), resp)
        assert resp.status == 401

        # Step 3: New password should work
        scope = _make_scope("/dashboard/api/settings/user/change-password", method="POST",
                            user_id=user["id"], email="flow@test.com", role="member")
        resp = _Capture()
        await handle_request(scope, _make_receive({
            "current_password": "pass2new",
            "new_password": "pass3",
        }), resp)
        assert resp.status == 200


class TestRegenerateOAuthWorkflow:
    """Test: regenerate secret → read instance settings → verify new secret is different."""

    @pytest.fixture(autouse=True)
    async def reset_oauth_keys(self, tmp_path, monkeypatch):
        import switchboard.auth.oauth as _oauth
        monkeypatch.setattr(_oauth, "OAUTH_RSA_KEY_PATH", str(tmp_path / "test_key.pem"))
        _oauth._rsa_private_key = None
        _oauth._rsa_public_jwk = None
        yield
        _oauth._rsa_private_key = None
        _oauth._rsa_public_jwk = None

    async def test_regenerate_then_read_shows_new_secret(self, db):
        from switchboard.auth.oauth import init_oauth_keys, seed_default_client
        from switchboard.dashboard.api import handle_request
        init_oauth_keys()
        await seed_default_client()

        # Step 1: Read original secret
        scope = _make_scope("/dashboard/api/settings/instance")
        resp = _Capture()
        with _patch_httpx(200, {}):
            await handle_request(scope, _make_receive(), resp)
        original_secret = resp.json()["oauth"]["client_secret"]

        # Step 2: Regenerate
        scope = _make_scope("/dashboard/api/settings/instance/regenerate-oauth-secret",
                            method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        new_secret = resp.json()["client_secret"]
        assert new_secret != original_secret

        # Step 3: Read again — should show the new secret
        scope = _make_scope("/dashboard/api/settings/instance")
        resp = _Capture()
        with _patch_httpx(200, {}):
            await handle_request(scope, _make_receive(), resp)
        read_secret = resp.json()["oauth"]["client_secret"]
        assert read_secret == new_secret


# ── Role-based access control integration ─────────────────────────────────────

class TestRoleBasedAccess:
    """Verify the frontend's role-based visibility logic is backed by the API."""

    async def test_member_can_access_user_settings(self, db):
        from switchboard.dashboard.api import handle_request

        user = await db.create_user(email="member@test.com", name="Member",
                                    role="member")
        scope = _make_scope("/dashboard/api/settings/user", user_id=user["id"],
                            email="member@test.com", role="member")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["profile"]["role"] == "member"

    async def test_member_role_in_response_prevents_instance_access(self, db):
        """The frontend checks profile.role to decide whether to show instance settings.
        Verify that a member's role is returned AND that instance endpoints return 403."""
        from switchboard.dashboard.api import handle_request

        user = await db.create_user(email="m2@test.com", name="M2", role="member")

        # Get user settings — should include role
        scope = _make_scope("/dashboard/api/settings/user", user_id=user["id"],
                            email="m2@test.com", role="member")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.json()["profile"]["role"] == "member"

        # Attempt instance settings — should be 403
        scope = _make_scope("/dashboard/api/settings/instance", role="member",
                            user_id=user["id"], email="m2@test.com")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 403

    async def test_admin_can_access_both(self, db):
        from switchboard.dashboard.api import handle_request

        user = await db.create_user(email="admin@test.com", name="Admin",
                                    role="admin")

        # User settings
        scope = _make_scope("/dashboard/api/settings/user", user_id=user["id"],
                            email="admin@test.com", role="admin")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        assert resp.json()["profile"]["role"] == "admin"

        # Instance settings
        scope = _make_scope("/dashboard/api/settings/instance", role="admin",
                            user_id=user["id"], email="admin@test.com")
        resp = _Capture()
        with _patch_httpx(200, {}):
            await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200

    async def test_member_cannot_patch_instance(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance", method="PATCH",
                            role="member")
        resp = _Capture()
        await handle_request(scope, _make_receive({"github_pat": "ghp_x"}), resp)
        assert resp.status == 403

    async def test_member_cannot_test_github(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance/test-github",
                            method="POST", role="member")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 403

    async def test_member_cannot_regenerate_secret(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance/regenerate-oauth-secret",
                            method="POST", role="member")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 403
