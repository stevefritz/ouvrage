"""Tests for git credentials settings API endpoints.

Covers all 4 new endpoints:
- GET  /dashboard/api/settings/git-credentials
- PUT  /dashboard/api/settings/git-credentials/{provider}
- DELETE /dashboard/api/settings/git-credentials/{provider}
- POST /dashboard/api/settings/git-credentials/{provider}/test
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
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    return mock_resp


def _patch_httpx(status_code: int, json_data: dict):
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=_mock_httpx_response(status_code, json_data))

    class _FakeCtx:
        async def __aenter__(self):
            return mock_client

        async def __aexit__(self, *args):
            return False

    return patch("switchboard.dashboard.api.httpx.AsyncClient", return_value=_FakeCtx())


# ── GET /settings/git-credentials ────────────────────────────────────────────

class TestGetGitCredentials:

    async def test_returns_three_providers_unconfigured(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert "credentials" in data
        creds = {c["provider"]: c for c in data["credentials"]}
        assert set(creds.keys()) == {"github", "gitlab", "bitbucket"}

        for provider in ("github", "gitlab", "bitbucket"):
            assert creds[provider]["configured"] is False
            assert creds[provider]["credential_last4"] is None
            assert creds[provider]["hostname_is_default"] is True

    async def test_returns_default_hostnames(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        creds = {c["provider"]: c for c in data["credentials"]}
        assert creds["github"]["hostname"] == "github.com"
        assert creds["gitlab"]["hostname"] == "gitlab.com"
        assert creds["bitbucket"]["hostname"] == "bitbucket.org"

    async def test_shows_configured_provider_last4(self, db):
        from switchboard.dashboard.api import handle_request

        await db.create_credential("github", "ghp_abcdefghij1234", "github.com")

        scope = _make_scope("/dashboard/api/settings/git-credentials")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        creds = {c["provider"]: c for c in data["credentials"]}
        assert creds["github"]["configured"] is True
        assert creds["github"]["credential_last4"] == "1234"

    async def test_shows_encrypted_credential_last4(self, db):
        from switchboard.dashboard.api import handle_request
        from switchboard.crypto import encrypt_value

        encrypted = encrypt_value("ghp_abcdefghij5678")
        await db.create_credential("github", encrypted, "github.com")

        scope = _make_scope("/dashboard/api/settings/git-credentials")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        creds = {c["provider"]: c for c in data["credentials"]}
        assert creds["github"]["credential_last4"] == "5678"

    async def test_custom_hostname_flagged_non_default(self, db):
        from switchboard.dashboard.api import handle_request

        await db.create_credential("gitlab", "glpat-xxxx", "gl.mycompany.com")

        scope = _make_scope("/dashboard/api/settings/git-credentials")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        creds = {c["provider"]: c for c in data["credentials"]}
        assert creds["gitlab"]["hostname"] == "gl.mycompany.com"
        assert creds["gitlab"]["hostname_is_default"] is False

    async def test_member_gets_403(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials", role="member")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 403

    async def test_admin_can_access(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials", role="admin")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200


# ── PUT /settings/git-credentials/{provider} ─────────────────────────────────

class TestPutGitCredential:

    async def test_save_github_credential(self, db):
        from switchboard.dashboard.api import handle_request
        from switchboard.crypto import decrypt_value, is_fernet_token

        scope = _make_scope("/dashboard/api/settings/git-credentials/github", method="PUT")
        resp = _Capture()
        await handle_request(scope, _make_receive({"credential": "ghp_mytoken12345"}), resp)

        assert resp.status == 200
        assert resp.json()["ok"] is True

        cred = await db.get_credential_by_provider("github")
        assert cred is not None
        raw = cred["credential"]
        assert is_fernet_token(raw)
        assert decrypt_value(raw) == "ghp_mytoken12345"

    async def test_save_gitlab_credential_with_custom_hostname(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials/gitlab", method="PUT")
        resp = _Capture()
        body = {"credential": "glpat-xxxxxxxx", "hostname": "gl.internal.io"}
        await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 200
        cred = await db.get_credential_by_provider("gitlab")
        assert cred["hostname"] == "gl.internal.io"

    async def test_update_existing_credential(self, db):
        from switchboard.dashboard.api import handle_request
        from switchboard.crypto import decrypt_value

        await db.create_credential("github", "ghp_old", "github.com")

        scope = _make_scope("/dashboard/api/settings/git-credentials/github", method="PUT")
        resp = _Capture()
        await handle_request(scope, _make_receive({"credential": "ghp_new99999"}), resp)

        assert resp.status == 200
        cred = await db.get_credential_by_provider("github")
        assert decrypt_value(cred["credential"]) == "ghp_new99999"

    async def test_invalid_provider_returns_400(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials/notreal", method="PUT")
        resp = _Capture()
        await handle_request(scope, _make_receive({"credential": "abc"}), resp)

        assert resp.status == 400

    async def test_missing_credential_returns_400(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials/github", method="PUT")
        resp = _Capture()
        await handle_request(scope, _make_receive({}), resp)

        assert resp.status == 400

    async def test_defaults_to_default_hostname(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials/bitbucket", method="PUT")
        resp = _Capture()
        await handle_request(scope, _make_receive({"credential": "user:pass"}), resp)

        assert resp.status == 200
        cred = await db.get_credential_by_provider("bitbucket")
        assert cred["hostname"] == "bitbucket.org"

    async def test_member_gets_403(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials/github", method="PUT", role="member")
        resp = _Capture()
        await handle_request(scope, _make_receive({"credential": "ghp_x"}), resp)

        assert resp.status == 403


# ── DELETE /settings/git-credentials/{provider} ──────────────────────────────

class TestDeleteGitCredential:

    async def test_delete_existing_credential(self, db):
        from switchboard.dashboard.api import handle_request

        await db.create_credential("github", "ghp_token", "github.com")

        scope = _make_scope("/dashboard/api/settings/git-credentials/github", method="DELETE")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        assert resp.json()["ok"] is True

        remaining = await db.get_credential_by_provider("github")
        assert remaining is None

    async def test_delete_nonexistent_returns_404(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials/gitlab", method="DELETE")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 404

    async def test_invalid_provider_returns_400(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials/notreal", method="DELETE")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 400

    async def test_member_gets_403(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials/github", method="DELETE", role="member")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 403


# ── POST /settings/git-credentials/{provider}/test ───────────────────────────

class TestTestGitCredential:

    async def test_no_credential_returns_invalid(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials/github/test", method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["valid"] is False
        assert "No github credential" in data["error"]

    async def test_github_valid_credential(self, db):
        from switchboard.dashboard.api import handle_request
        from switchboard.crypto import encrypt_value

        await db.create_credential("github", encrypt_value("ghp_test1234"), "github.com")

        scope = _make_scope("/dashboard/api/settings/git-credentials/github/test", method="POST")
        resp = _Capture()

        with _patch_httpx(200, {"login": "octocat"}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["username"] == "octocat"

    async def test_github_invalid_credential(self, db):
        from switchboard.dashboard.api import handle_request
        from switchboard.crypto import encrypt_value

        await db.create_credential("github", encrypt_value("ghp_bad"), "github.com")

        scope = _make_scope("/dashboard/api/settings/git-credentials/github/test", method="POST")
        resp = _Capture()

        with _patch_httpx(401, {"message": "Bad credentials"}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["valid"] is False
        assert "401" in data["error"]

    async def test_gitlab_valid_credential(self, db):
        from switchboard.dashboard.api import handle_request
        from switchboard.crypto import encrypt_value

        await db.create_credential("gitlab", encrypt_value("glpat-xxxx"), "gitlab.com")

        scope = _make_scope("/dashboard/api/settings/git-credentials/gitlab/test", method="POST")
        resp = _Capture()

        with _patch_httpx(200, {"username": "gitlabuser"}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["username"] == "gitlabuser"

    async def test_bitbucket_valid_credential(self, db):
        from switchboard.dashboard.api import handle_request
        from switchboard.crypto import encrypt_value

        await db.create_credential("bitbucket", encrypt_value("alice:myapppassword"), "bitbucket.org")

        scope = _make_scope("/dashboard/api/settings/git-credentials/bitbucket/test", method="POST")
        resp = _Capture()

        with _patch_httpx(200, {"username": "alice"}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["username"] == "alice"

    async def test_bitbucket_missing_colon_in_credential(self, db):
        from switchboard.dashboard.api import handle_request
        from switchboard.crypto import encrypt_value

        # credential without colon is invalid for Bitbucket
        await db.create_credential("bitbucket", encrypt_value("nocoLonatall"), "bitbucket.org")

        scope = _make_scope("/dashboard/api/settings/git-credentials/bitbucket/test", method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["valid"] is False
        assert "username:app_password" in data["error"]

    async def test_invalid_provider_returns_400(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials/notreal/test", method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 400

    async def test_member_gets_403(self, db):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/settings/git-credentials/github/test", method="POST", role="member")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)

        assert resp.status == 403

    async def test_unencrypted_credential_also_works(self, db):
        from switchboard.dashboard.api import handle_request

        # Credential stored without encryption (edge case — legacy data)
        await db.create_credential("github", "ghp_plaintext1234", "github.com")

        scope = _make_scope("/dashboard/api/settings/git-credentials/github/test", method="POST")
        resp = _Capture()

        with _patch_httpx(200, {"login": "testuser"}):
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["username"] == "testuser"
