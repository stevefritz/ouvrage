"""Tests for OAuth 2.0 Authorization Server.

Covers: OIDC discovery, JWKS, auth code flow, token exchange, refresh with rotation,
revocation (RFC 7009), PKCE S256, invalid client, expired code.
"""

import base64
import hashlib
import json
import os
import secrets
import time

import jwt as pyjwt
import pytest

from switchboard.auth.oauth import (
    init_oauth_keys,
    get_openid_configuration,
    get_jwks,
    get_client,
    validate_client_secret,
    create_authorization_code,
    consume_authorization_code,
    issue_tokens,
    refresh_access_token,
    revoke_token,
    verify_pkce,
    seed_default_client,
    _get_base_url,
    _rsa_private_key,
    _rsa_public_jwk,
    SUPPORTED_SCOPES,
    RSA_KID,
    ACCESS_TOKEN_TTL,
)


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def oauth_env(tmp_path):
    """Set up OAuth env vars for tests."""
    os.environ["OAUTH_BASE_URL"] = "https://switchboard.test"
    os.environ["OAUTH_RSA_KEY_PATH"] = str(tmp_path / "test_rsa_key.pem")
    # Reload settings module to pick up new env vars
    import switchboard.config.settings as _s
    _s.OAUTH_BASE_URL = os.environ["OAUTH_BASE_URL"]
    _s.OAUTH_RSA_KEY_PATH = os.environ["OAUTH_RSA_KEY_PATH"]

    # Also patch the module-level references in oauth.py
    import switchboard.auth.oauth as _oauth
    _oauth.OAUTH_BASE_URL = os.environ["OAUTH_BASE_URL"]
    _oauth.OAUTH_RSA_KEY_PATH = os.environ["OAUTH_RSA_KEY_PATH"]
    # Reset cached keys
    _oauth._rsa_private_key = None
    _oauth._rsa_public_jwk = None

    yield

    # Cleanup
    os.environ.pop("OAUTH_BASE_URL", None)
    os.environ.pop("OAUTH_RSA_KEY_PATH", None)


@pytest.fixture
def rsa_keys(oauth_env):
    """Initialize RSA keys for tests."""
    init_oauth_keys()


@pytest.fixture
async def seeded_client(db, rsa_keys):
    """DB with seeded claude-mcp client."""
    await seed_default_client()
    return await get_client("claude-mcp")


@pytest.fixture
async def test_user(db):
    """Create a test user."""
    return await db.create_user(
        email="test@example.com",
        name="Test User",
    )


# ── OIDC Discovery ────────────────────────────────────────────────────────


# ── JWKS ──────────────────────────────────────────────────────────────────

class TestJWKS:


    def test_jwks_key_persists(self, rsa_keys, oauth_env):
        """Key should be loadable from disk on second init."""
        jwks1 = get_jwks()
        # Re-init (should load from disk)
        import switchboard.auth.oauth as _oauth
        _oauth._rsa_private_key = None
        _oauth._rsa_public_jwk = None
        init_oauth_keys()
        jwks2 = get_jwks()
        assert jwks1["keys"][0]["n"] == jwks2["keys"][0]["n"]


# ── Client Management ─────────────────────────────────────────────────────

class TestClientManagement:
    async def test_seed_default_client(self, seeded_client):
        assert seeded_client is not None
        assert seeded_client["client_id"] == "claude-mcp"
        assert seeded_client["client_name"] == "Claude MCP Client"
        assert seeded_client["token_endpoint_auth_method"] == "client_secret_post"
        assert seeded_client["consent_mode"] == "implicit"

    async def test_client_redirect_uris(self, seeded_client):
        uris = seeded_client["redirect_uris"]
        assert "https://claude.ai/oauth/callback" in uris
        assert "https://claude.ai/api/auth/oauth/callback" in uris
        assert "https://claude.ai/api/mcp/auth_callback" in uris

    async def test_client_scopes(self, seeded_client):
        scopes = seeded_client["scopes"]
        for s in SUPPORTED_SCOPES:
            assert s in scopes

    async def test_client_grant_types(self, seeded_client):
        assert "authorization_code" in seeded_client["grant_types"]
        assert "refresh_token" in seeded_client["grant_types"]


    async def test_seed_idempotent(self, seeded_client):
        """Seeding again should not create duplicates."""
        await seed_default_client()
        client = await get_client("claude-mcp")
        assert client is not None


# ── PKCE ──────────────────────────────────────────────────────────────────

class TestPKCE:


    def test_pkce_unsupported_method(self):
        assert not verify_pkce("verifier", "challenge", "plain")


# ── Authorization Code ────────────────────────────────────────────────────

class TestAuthorizationCode:

    async def test_code_single_use(self, seeded_client, test_user):
        code = await create_authorization_code(
            client_id="claude-mcp",
            user_id=test_user["id"],
            redirect_uri="https://claude.ai/oauth/callback",
            scope="openid",
        )
        # First consume succeeds
        assert await consume_authorization_code(code) is not None
        # Second consume fails
        assert await consume_authorization_code(code) is None

    async def test_expired_code(self, seeded_client, test_user):
        """Expired codes should be rejected."""
        code = secrets.token_urlsafe(32)
        # Insert with past expiry
        from switchboard.db.connection import get_db
        async with get_db() as db:
            await db.execute(
                """INSERT INTO oauth_authorization_codes
                   (code, client_id, user_id, redirect_uri, scope, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (code, "claude-mcp", test_user["id"],
                 "https://claude.ai/oauth/callback", "openid",
                 int(time.time()) - 100),
            )
            await db.commit()

        assert await consume_authorization_code(code) is None


# ── Token Issuance ────────────────────────────────────────────────────────


# ── Token Refresh ─────────────────────────────────────────────────────────

class TestTokenRefresh:

    async def test_refresh_token_rotation(self, seeded_client, test_user, rsa_keys):
        """Old refresh token should be revoked after rotation."""
        initial = await issue_tokens(
            client_id="claude-mcp",
            user_id=test_user["id"],
            scope="openid",
        )

        # First refresh succeeds
        refreshed = await refresh_access_token(
            initial["refresh_token"], "claude-mcp"
        )
        assert refreshed is not None

        # Second refresh with old token fails (revoked)
        second = await refresh_access_token(
            initial["refresh_token"], "claude-mcp"
        )
        assert second is None


# ── Token Revocation ──────────────────────────────────────────────────────


# ── ASGI Handler Integration Tests ────────────────────────────────────────

class TestASGIHandlers:
    """Test OAuth endpoints via the ASGI handler functions directly."""

    async def _call_handler(self, handler, method="GET", path="/", query="", body=b""):
        """Helper to call an ASGI handler and capture the response."""
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query.encode() if isinstance(query, str) else query,
            "headers": [],
        }

        response_started = False
        status = None
        headers = {}
        response_body = b""

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            nonlocal response_started, status, headers, response_body
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                for k, v in message.get("headers", []):
                    headers[k if isinstance(k, str) else k.decode()] = v if isinstance(v, str) else v.decode()
            elif message["type"] == "http.response.body":
                response_body += message.get("body", b"")

        await handler(scope, receive, send)
        return status, headers, response_body

    async def test_openid_configuration_endpoint(self, rsa_keys):
        from switchboard.auth.oauth import handle_openid_configuration
        status, headers, body = await self._call_handler(handle_openid_configuration)
        assert status == 200
        data = json.loads(body)
        assert data["issuer"] == "https://switchboard.test"

    async def test_jwks_endpoint(self, rsa_keys):
        from switchboard.auth.oauth import handle_jwks
        status, headers, body = await self._call_handler(handle_jwks)
        assert status == 200
        data = json.loads(body)
        assert len(data["keys"]) == 1
        assert data["keys"][0]["kid"] == RSA_KID


    async def test_authorize_invalid_client(self, db, rsa_keys):
        from switchboard.auth.oauth import handle_authorize
        query = "response_type=code&client_id=bogus&redirect_uri=https://example.com&scope=openid"
        status, _, body = await self._call_handler(
            handle_authorize, path="/oauth/authorize", query=query
        )
        assert status == 400
        assert json.loads(body)["error"] == "invalid_client"

    async def test_authorize_invalid_redirect_uri(self, seeded_client):
        from switchboard.auth.oauth import handle_authorize
        query = "response_type=code&client_id=claude-mcp&redirect_uri=https://evil.com/callback&scope=openid"
        status, _, body = await self._call_handler(
            handle_authorize, path="/oauth/authorize", query=query
        )
        assert status == 400
        assert json.loads(body)["error"] == "invalid_request"


    async def test_token_exchange_with_pkce(self, seeded_client, test_user, rsa_keys):
        """Auth code flow with PKCE S256."""
        from switchboard.auth.oauth import handle_token
        from switchboard.crypto import decrypt_value

        # Generate PKCE
        verifier = secrets.token_urlsafe(32)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        code = await create_authorization_code(
            client_id="claude-mcp",
            user_id=test_user["id"],
            redirect_uri="https://claude.ai/oauth/callback",
            scope="openid",
            code_challenge=challenge,
            code_challenge_method="S256",
        )

        client = await get_client("claude-mcp")
        client_secret = decrypt_value(client["client_secret_encrypted"])

        body = (
            f"grant_type=authorization_code&code={code}"
            f"&client_id=claude-mcp&client_secret={client_secret}"
            f"&redirect_uri=https://claude.ai/oauth/callback"
            f"&code_verifier={verifier}"
        ).encode()

        status, _, resp_body = await self._call_handler(
            handle_token, method="POST", path="/oauth/token", body=body
        )
        assert status == 200
        data = json.loads(resp_body)
        assert "access_token" in data

    async def test_token_exchange_pkce_missing_verifier(self, seeded_client, test_user, rsa_keys):
        """PKCE code without verifier should fail."""
        from switchboard.auth.oauth import handle_token
        from switchboard.crypto import decrypt_value

        verifier = secrets.token_urlsafe(32)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        code = await create_authorization_code(
            client_id="claude-mcp",
            user_id=test_user["id"],
            redirect_uri="https://claude.ai/oauth/callback",
            scope="openid",
            code_challenge=challenge,
            code_challenge_method="S256",
        )

        client = await get_client("claude-mcp")
        client_secret = decrypt_value(client["client_secret_encrypted"])

        # No code_verifier
        body = (
            f"grant_type=authorization_code&code={code}"
            f"&client_id=claude-mcp&client_secret={client_secret}"
            f"&redirect_uri=https://claude.ai/oauth/callback"
        ).encode()

        status, _, resp_body = await self._call_handler(
            handle_token, method="POST", path="/oauth/token", body=body
        )
        assert status == 400
        assert json.loads(resp_body)["error"] == "invalid_request"

    async def test_token_invalid_client_secret(self, seeded_client, test_user, rsa_keys):
        from switchboard.auth.oauth import handle_token

        code = await create_authorization_code(
            client_id="claude-mcp",
            user_id=test_user["id"],
            redirect_uri="https://claude.ai/oauth/callback",
            scope="openid",
        )

        body = (
            f"grant_type=authorization_code&code={code}"
            f"&client_id=claude-mcp&client_secret=wrong-secret"
        ).encode()

        status, _, resp_body = await self._call_handler(
            handle_token, method="POST", path="/oauth/token", body=body
        )
        assert status == 401
        assert json.loads(resp_body)["error"] == "invalid_client"

    async def test_revoke_endpoint(self, seeded_client, test_user, rsa_keys):
        from switchboard.auth.oauth import handle_revoke

        tokens = await issue_tokens(
            client_id="claude-mcp",
            user_id=test_user["id"],
            scope="openid",
        )

        body = f"token={tokens['refresh_token']}".encode()
        status, _, resp_body = await self._call_handler(
            handle_revoke, method="POST", path="/oauth/revoke", body=body
        )
        # RFC 7009: always 200
        assert status == 200

    async def test_revoke_unknown_token_still_200(self, db, rsa_keys):
        from switchboard.auth.oauth import handle_revoke

        body = b"token=nonexistent-token"
        status, _, _ = await self._call_handler(
            handle_revoke, method="POST", path="/oauth/revoke", body=body
        )
        assert status == 200
