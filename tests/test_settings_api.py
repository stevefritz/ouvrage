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

    async def test_github_not_connected_when_no_pat(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["github"]["connected"] is False

    async def test_github_not_connected_when_api_fails(self, db):
        from switchboard.dashboard.api import handle_request

        await db.set_instance_github_pat("ghp_badtoken")
        scope = _make_scope("/dashboard/api/settings/instance")
        resp = _Capture()

        with _patch_httpx(401, {"message": "Bad credentials"}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["github"]["connected"] is False
        # PAT IS stored — last4 should still be visible even when GitHub rejects it
        assert data["github"]["pat_last4"] == "oken"

    async def test_includes_oauth_info(self, db):
        from switchboard.dashboard.api import handle_request
        from switchboard.auth.oauth import seed_default_client, init_oauth_keys
        init_oauth_keys()
        await seed_default_client()

        scope = _make_scope("/dashboard/api/settings/instance")
        resp = _Capture()

        with _patch_httpx(200, {}):
            await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert "oauth" in data
        assert data["oauth"].get("client_id") == "claude-mcp"
        assert data["oauth"].get("client_secret") is not None

    async def test_member_gets_403(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance", role="member")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 403

    async def test_viewer_gets_403(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance", role="viewer")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 403

    async def test_admin_can_access(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance", role="admin")
        resp = _Capture()

        with _patch_httpx(200, {}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200


class TestPatchInstanceSettings:

    async def test_owner_can_set_pat(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance", method="PATCH")
        resp = _Capture()
        body = {"github_pat": "ghp_newtoken5678"}

        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 200
        assert resp.json()["ok"] is True

        # Verify PAT was stored (decrypted value matches)
        pat = await db.get_instance_github_pat()
        assert pat == "ghp_newtoken5678"

    async def test_pat_is_stored_encrypted(self, db):
        import switchboard.db.connection as _conn
        from switchboard.crypto import is_fernet_token
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"github_pat": "ghp_plain"}), resp)

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT github_pat_encrypted FROM instance WHERE id = 1"
            )
        assert is_fernet_token(rows[0]["github_pat_encrypted"])

    async def test_member_gets_403(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance", method="PATCH", role="member")
        resp = _Capture()

        await handle_request(scope, _make_receive({"github_pat": "ghp_x"}), resp)

        assert resp.status == 403


class TestTestGithub:

    async def test_valid_pat_returns_username(self, db):
        from switchboard.dashboard.api import handle_request

        await db.set_instance_github_pat("ghp_validtoken")
        scope = _make_scope("/dashboard/api/settings/instance/test-github", method="POST")
        resp = _Capture()

        with _patch_httpx(200, {"login": "testuser"}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["username"] == "testuser"

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

    async def test_owner_gets_new_secret(self, db):
        from switchboard.dashboard.api import handle_request
        from switchboard.auth.oauth import seed_default_client, init_oauth_keys
        init_oauth_keys()
        await seed_default_client()

        scope = _make_scope("/dashboard/api/settings/instance/regenerate-oauth-secret",
                            method="POST")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["client_id"] == "claude-mcp"
        assert "client_secret" in data
        assert len(data["client_secret"]) > 10

    async def test_returns_404_when_oauth_client_not_seeded(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance/regenerate-oauth-secret",
                            method="POST")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 404

    async def test_new_secret_is_different_from_old(self, db):
        from switchboard.dashboard.api import handle_request
        from switchboard.auth.oauth import get_client, seed_default_client, init_oauth_keys
        from switchboard.crypto import decrypt_value, is_fernet_token
        init_oauth_keys()
        await seed_default_client()

        # Get old secret
        old_client = await get_client("claude-mcp")
        old_raw = old_client["client_secret_encrypted"]
        old_secret = decrypt_value(old_raw) if is_fernet_token(old_raw) else old_raw

        scope = _make_scope("/dashboard/api/settings/instance/regenerate-oauth-secret",
                            method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        new_secret = resp.json()["client_secret"]
        assert new_secret != old_secret

    async def test_new_secret_stored_encrypted(self, db):
        from switchboard.dashboard.api import handle_request
        from switchboard.auth.oauth import get_client, seed_default_client, init_oauth_keys
        from switchboard.crypto import is_fernet_token
        init_oauth_keys()
        await seed_default_client()

        scope = _make_scope("/dashboard/api/settings/instance/regenerate-oauth-secret",
                            method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        client = await get_client("claude-mcp")
        assert is_fernet_token(client["client_secret_encrypted"])

    async def test_member_gets_403(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/instance/regenerate-oauth-secret",
                            method="POST", role="member")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 403


# ── User settings ─────────────────────────────────────────────────────────────

class TestGetUserSettings:

    async def test_returns_profile_info(self, db):
        from switchboard.dashboard.api import handle_request

        # Use the seeded owner user
        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"],
                            email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["profile"]["email"] == "owner@localhost"
        assert data["profile"]["role"] == "owner"
        assert "name" in data["profile"]
        assert "timezone" in data["profile"]

    async def test_anthropic_not_configured_when_no_key(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"],
                            email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert data["anthropic"]["configured"] is False
        assert data["anthropic"]["key_last4"] is None

    async def test_anthropic_key_last4_when_configured(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        await db.update_user_credentials(owner["id"], anthropic_api_key="sk-ant-xK3mABC")
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"],
                            email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert data["anthropic"]["configured"] is True
        assert data["anthropic"]["key_last4"] == "mABC"

    async def test_full_key_not_returned(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        await db.update_user_credentials(owner["id"], anthropic_api_key="sk-ant-secret")
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"],
                            email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        # Verify no field exposes the full key
        body = resp.body.decode()
        assert "sk-ant-secret" not in body

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

    async def test_has_password_false_when_no_password_hash(self, db):
        """SSO/SaaS users with no local password_hash get has_password=false."""
        from switchboard.dashboard.api import handle_request

        # owner@localhost is created without a password hash
        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"],
                            email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert data["profile"]["has_password"] is False

    async def test_has_password_true_when_password_hash_set(self, db):
        """Standalone users with a local password get has_password=true."""
        from argon2 import PasswordHasher
        from switchboard.dashboard.api import handle_request

        ph = PasswordHasher()
        user = await db.create_user(
            email="local@test.com", name="Local User", role="member",
            password_hash=ph.hash("mypassword"),
        )
        scope = _make_scope("/dashboard/api/settings/user", user_id=user["id"],
                            email="local@test.com")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert data["profile"]["has_password"] is True


class TestGetUserSettingsGitCredential:
    """GET /dashboard/api/settings/user — git_credential field reflects any provider in git_credentials table."""

    async def test_git_credential_not_configured_by_default(self, db):
        """With no git credentials in the table, git_credential.configured is False."""
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"],
                            email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert "git_credential" in data
        assert data["git_credential"]["configured"] is False

    async def test_git_credential_configured_when_github_set(self, db):
        """After adding a GitHub credential, git_credential.configured is True."""
        from switchboard.dashboard.api import handle_request

        await db.create_credential(
            provider="github",
            credential="encrypted-fake",
            hostname="github.com",
            credential_last4="1234",
        )

        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"],
                            email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert data["git_credential"]["configured"] is True

    async def test_git_credential_configured_for_non_github_provider(self, db):
        """A GitLab or Bitbucket credential also satisfies git_credential.configured."""
        from switchboard.dashboard.api import handle_request

        await db.create_credential(
            provider="gitlab",
            credential="encrypted-fake-gitlab",
            hostname="gitlab.com",
            credential_last4="abcd",
        )

        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user", user_id=owner["id"],
                            email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert data["git_credential"]["configured"] is True


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

    async def test_update_timezone(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user", method="PATCH",
                            user_id=owner["id"], email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive({"timezone": "America/New_York"}), resp)

        assert resp.status == 200
        updated = await db.get_user(owner["id"])
        assert updated["timezone"] == "America/New_York"

    async def test_update_anthropic_key(self, db):
        from switchboard.dashboard.api import handle_request
        from switchboard.crypto import is_fernet_token
        import switchboard.db.connection as _conn

        owner = await db.get_user_by_email("owner@localhost")
        scope = _make_scope("/dashboard/api/settings/user", method="PATCH",
                            user_id=owner["id"], email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive({"anthropic_api_key": "sk-ant-newkey"}), resp)

        assert resp.status == 200

        # Verify it's stored encrypted
        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT anthropic_api_key FROM user_credentials WHERE user_id = ?",
                (owner["id"],),
            )
        assert rows and is_fernet_token(rows[0]["anthropic_api_key"])

        # Verify it decrypts correctly
        key = await db.get_anthropic_key(owner["id"])
        assert key == "sk-ant-newkey"

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

    async def test_empty_body_is_noop(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        original = await db.get_user(owner["id"])
        scope = _make_scope("/dashboard/api/settings/user", method="PATCH",
                            user_id=owner["id"], email="owner@localhost")
        resp = _Capture()

        await handle_request(scope, _make_receive({}), resp)

        assert resp.status == 200
        unchanged = await db.get_user(owner["id"])
        assert unchanged["name"] == original["name"]


class TestTestAnthropic:

    async def test_valid_key_returns_true(self, db):
        from switchboard.dashboard.api import handle_request

        owner = await db.get_user_by_email("owner@localhost")
        await db.update_user_credentials(owner["id"], anthropic_api_key="sk-ant-valid")
        scope = _make_scope("/dashboard/api/settings/user/test-anthropic", method="POST",
                            user_id=owner["id"], email="owner@localhost")
        resp = _Capture()

        with _patch_httpx(200, {"data": []}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        assert resp.json()["valid"] is True

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

    async def test_correct_current_password_updates(self, db):
        from switchboard.dashboard.api import handle_request

        user = await self._create_user_with_password(db, "pw@test.com", "oldpass123")
        scope = _make_scope("/dashboard/api/settings/user/change-password", method="POST",
                            user_id=user["id"], email="pw@test.com", role="member")
        resp = _Capture()

        await handle_request(
            scope,
            _make_receive({"current_password": "oldpass123", "new_password": "newpass456"}),
            resp,
        )

        assert resp.status == 200
        assert resp.json()["ok"] is True

    async def test_correct_password_allows_login_with_new(self, db):
        from switchboard.dashboard.api import handle_request
        from argon2 import PasswordHasher

        user = await self._create_user_with_password(db, "pw2@test.com", "original")
        scope = _make_scope("/dashboard/api/settings/user/change-password", method="POST",
                            user_id=user["id"], email="pw2@test.com", role="member")
        resp = _Capture()

        await handle_request(
            scope,
            _make_receive({"current_password": "original", "new_password": "updated"}),
            resp,
        )

        # New password should work
        full = await db.get_user_by_email_with_auth("pw2@test.com")
        ph = PasswordHasher()
        assert ph.verify(full["password_hash"], "updated")

    async def test_wrong_current_password_returns_401(self, db):
        from switchboard.dashboard.api import handle_request

        user = await self._create_user_with_password(db, "pw3@test.com", "rightpass")
        scope = _make_scope("/dashboard/api/settings/user/change-password", method="POST",
                            user_id=user["id"], email="pw3@test.com", role="member")
        resp = _Capture()

        await handle_request(
            scope,
            _make_receive({"current_password": "wrongpass", "new_password": "newpass"}),
            resp,
        )

        assert resp.status == 401

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
