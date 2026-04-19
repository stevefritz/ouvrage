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

from ouvrage.auth.oauth import (
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
    os.environ["OAUTH_BASE_URL"] = "https://ouvrage.test"
    os.environ["OAUTH_RSA_KEY_PATH"] = str(tmp_path / "test_rsa_key.pem")
    # Reload settings module to pick up new env vars
    import ouvrage.config.settings as _s
    _s.OAUTH_BASE_URL = os.environ["OAUTH_BASE_URL"]
    _s.OAUTH_RSA_KEY_PATH = os.environ["OAUTH_RSA_KEY_PATH"]

    # Also patch the module-level references in oauth.py
    import ouvrage.auth.oauth as _oauth
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

class TestOIDCDiscovery:
    def test_discovery_has_required_fields(self, rsa_keys):
        config = get_openid_configuration()
        assert config["issuer"] == "https://ouvrage.test"
        assert config["authorization_endpoint"] == "https://ouvrage.test/oauth/authorize"
        assert config["token_endpoint"] == "https://ouvrage.test/oauth/token"
        assert config["revocation_endpoint"] == "https://ouvrage.test/oauth/revoke"
        assert config["jwks_uri"] == "https://ouvrage.test/jwks"

    def test_discovery_code_challenge_methods(self, rsa_keys):
        config = get_openid_configuration()
        assert "S256" in config["code_challenge_methods_supported"]

    def test_discovery_supported_scopes(self, rsa_keys):
        config = get_openid_configuration()
        for scope in ["openid", "profile", "email", "offline_access"]:
            assert scope in config["scopes_supported"]

    def test_discovery_rs256(self, rsa_keys):
        config = get_openid_configuration()
        assert "RS256" in config["id_token_signing_alg_values_supported"]

    def test_discovery_client_secret_post(self, rsa_keys):
        config = get_openid_configuration()
        assert "client_secret_post" in config["token_endpoint_auth_methods_supported"]


# ── JWKS ──────────────────────────────────────────────────────────────────

class TestJWKS:
    def test_jwks_has_keys(self, rsa_keys):
        jwks = get_jwks()
        assert "keys" in jwks
        assert len(jwks["keys"]) == 1

    def test_jwks_key_properties(self, rsa_keys):
        key = get_jwks()["keys"][0]
        assert key["kty"] == "RSA"
        assert key["kid"] == RSA_KID
        assert key["use"] == "sig"
        assert key["alg"] == "RS256"
        assert "n" in key
        assert "e" in key

    def test_jwks_key_persists(self, rsa_keys, oauth_env):
        """Key should be loadable from disk on second init."""
        jwks1 = get_jwks()
        # Re-init (should load from disk)
        import ouvrage.auth.oauth as _oauth
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

    async def test_invalid_client(self, db, rsa_keys):
        client = await get_client("nonexistent")
        assert client is None

    async def test_validate_client_secret(self, seeded_client):
        """Seeded client should have a valid encrypted secret."""
        from ouvrage.crypto import decrypt_value
        secret = decrypt_value(seeded_client["client_secret_encrypted"])
        assert await validate_client_secret("claude-mcp", secret)
        assert not await validate_client_secret("claude-mcp", "wrong-secret")

    async def test_seed_idempotent(self, seeded_client):
        """Seeding again should not create duplicates."""
        await seed_default_client()
        client = await get_client("claude-mcp")
        assert client is not None


# ── PKCE ──────────────────────────────────────────────────────────────────

class TestPKCE:
    def test_pkce_s256_valid(self):
        verifier = secrets.token_urlsafe(32)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert verify_pkce(verifier, challenge, "S256")

    def test_pkce_s256_invalid(self):
        assert not verify_pkce("wrong-verifier", "some-challenge", "S256")

    def test_pkce_unsupported_method(self):
        assert not verify_pkce("verifier", "challenge", "plain")


# ── Authorization Code ────────────────────────────────────────────────────

class TestAuthorizationCode:
    async def test_create_and_consume(self, seeded_client, test_user):
        code = await create_authorization_code(
            client_id="claude-mcp",
            user_id=test_user["id"],
            redirect_uri="https://claude.ai/oauth/callback",
            scope="openid profile email",
        )
        assert code is not None

        code_data = await consume_authorization_code(code)
        assert code_data is not None
        assert code_data["client_id"] == "claude-mcp"
        assert code_data["user_id"] == test_user["id"]
        assert code_data["scope"] == "openid profile email"

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
        from ouvrage.db.connection import get_db
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

    async def test_code_with_pkce(self, seeded_client, test_user):
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
        code_data = await consume_authorization_code(code)
        assert code_data["code_challenge"] == challenge
        assert code_data["code_challenge_method"] == "S256"
        assert verify_pkce(verifier, code_data["code_challenge"], code_data["code_challenge_method"])


# ── Token Issuance ────────────────────────────────────────────────────────

class TestTokenIssuance:
    async def test_issue_tokens(self, seeded_client, test_user, rsa_keys):
        result = await issue_tokens(
            client_id="claude-mcp",
            user_id=test_user["id"],
            scope="openid profile email",
            email="test@example.com",
            name="Test User",
        )

        assert "access_token" in result
        assert result["token_type"] == "Bearer"
        assert result["expires_in"] == ACCESS_TOKEN_TTL
        assert "refresh_token" in result
        assert result["scope"] == "openid profile email"

    async def test_access_token_is_valid_jwt(self, seeded_client, test_user, rsa_keys):
        result = await issue_tokens(
            client_id="claude-mcp",
            user_id=test_user["id"],
            scope="openid profile email",
            email="test@example.com",
            name="Test User",
        )

        # Decode and verify the JWT
        jwks = get_jwks()
        key_data = jwks["keys"][0]
        pub_key = pyjwt.algorithms.RSAAlgorithm.from_jwk(key_data)

        claims = pyjwt.decode(
            result["access_token"],
            pub_key,
            algorithms=["RS256"],
            audience="claude-mcp",
        )

        assert claims["iss"] == "https://ouvrage.test"
        assert claims["sub"] == str(test_user["id"])
        assert claims["aud"] == "claude-mcp"
        assert claims["scope"] == "openid profile email"
        assert claims["email"] == "test@example.com"
        assert claims["name"] == "Test User"
        assert "jti" in claims
        assert "exp" in claims
        assert "iat" in claims

    async def test_jwt_kid_header(self, seeded_client, test_user, rsa_keys):
        result = await issue_tokens(
            client_id="claude-mcp",
            user_id=test_user["id"],
            scope="openid",
        )
        header = pyjwt.get_unverified_header(result["access_token"])
        assert header["kid"] == RSA_KID
        assert header["alg"] == "RS256"


# ── Token Refresh ─────────────────────────────────────────────────────────

class TestTokenRefresh:
    async def test_refresh_token_exchange(self, seeded_client, test_user, rsa_keys):
        # Issue initial tokens
        initial = await issue_tokens(
            client_id="claude-mcp",
            user_id=test_user["id"],
            scope="openid profile",
            email="test@example.com",
        )

        # Refresh
        refreshed = await refresh_access_token(
            initial["refresh_token"], "claude-mcp"
        )
        assert refreshed is not None
        assert refreshed["access_token"] != initial["access_token"]
        assert refreshed["refresh_token"] != initial["refresh_token"]
        assert refreshed["scope"] == "openid profile"

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

    async def test_refresh_wrong_client(self, seeded_client, test_user, rsa_keys):
        initial = await issue_tokens(
            client_id="claude-mcp",
            user_id=test_user["id"],
            scope="openid",
        )
        result = await refresh_access_token(
            initial["refresh_token"], "wrong-client"
        )
        assert result is None

    async def test_refresh_invalid_token(self, seeded_client, rsa_keys):
        result = await refresh_access_token("bogus-token", "claude-mcp")
        assert result is None


# ── Token Revocation ──────────────────────────────────────────────────────

class TestTokenRevocation:
    async def test_revoke_refresh_token(self, seeded_client, test_user, rsa_keys):
        tokens = await issue_tokens(
            client_id="claude-mcp",
            user_id=test_user["id"],
            scope="openid",
        )

        result = await revoke_token(tokens["refresh_token"])
        assert result is True

        # Refresh should now fail
        refreshed = await refresh_access_token(
            tokens["refresh_token"], "claude-mcp"
        )
        assert refreshed is None

    async def test_revoke_access_token(self, seeded_client, test_user, rsa_keys):
        tokens = await issue_tokens(
            client_id="claude-mcp",
            user_id=test_user["id"],
            scope="openid",
        )

        result = await revoke_token(tokens["access_token"], "access_token")
        assert result is True

    async def test_revoke_unknown_token(self, seeded_client, rsa_keys):
        result = await revoke_token("nonexistent-token")
        assert result is False


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
        from ouvrage.auth.oauth import handle_openid_configuration
        status, headers, body = await self._call_handler(handle_openid_configuration)
        assert status == 200
        data = json.loads(body)
        assert data["issuer"] == "https://ouvrage.test"

    async def test_jwks_endpoint(self, rsa_keys):
        from ouvrage.auth.oauth import handle_jwks
        status, headers, body = await self._call_handler(handle_jwks)
        assert status == 200
        data = json.loads(body)
        assert len(data["keys"]) == 1
        assert data["keys"][0]["kid"] == RSA_KID

    async def test_authorize_no_session_redirects_to_login(self, seeded_client):
        from ouvrage.auth.oauth import handle_authorize
        query = "response_type=code&client_id=claude-mcp&redirect_uri=https://claude.ai/oauth/callback&scope=openid"
        status, headers, body = await self._call_handler(
            handle_authorize, path="/oauth/authorize", query=query
        )
        # No session → redirect to login page (not 401 anymore)
        assert status == 302
        location = headers.get("location", "")
        assert location.startswith("/dashboard/login?next=")
        # oauth/authorize appears URL-encoded in the next= param
        assert "oauth" in location and "authorize" in location

    async def test_authorize_with_session(self, seeded_client, test_user):
        from ouvrage.auth.oauth import handle_authorize
        query = "response_type=code&client_id=claude-mcp&redirect_uri=https://claude.ai/oauth/callback&scope=openid&state=xyz"

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/oauth/authorize",
            "query_string": query.encode(),
            "headers": [],
            "oauth_user_id": test_user["id"],
        }

        status = None
        location = None

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            nonlocal status, location
            if message["type"] == "http.response.start":
                status = message["status"]
                for k, v in message.get("headers", []):
                    key = k.decode() if isinstance(k, bytes) else k
                    val = v.decode() if isinstance(v, bytes) else v
                    if key == "location":
                        location = val

        await handle_authorize(scope, receive, send)
        assert status == 302
        assert location is not None
        assert "code=" in location
        assert "state=xyz" in location
        assert location.startswith("https://claude.ai/oauth/callback")

    async def test_authorize_invalid_client(self, db, rsa_keys):
        from ouvrage.auth.oauth import handle_authorize
        query = "response_type=code&client_id=bogus&redirect_uri=https://example.com&scope=openid"
        status, _, body = await self._call_handler(
            handle_authorize, path="/oauth/authorize", query=query
        )
        assert status == 400
        assert json.loads(body)["error"] == "invalid_client"

    async def test_authorize_invalid_redirect_uri(self, seeded_client):
        from ouvrage.auth.oauth import handle_authorize
        query = "response_type=code&client_id=claude-mcp&redirect_uri=https://evil.com/callback&scope=openid"
        status, _, body = await self._call_handler(
            handle_authorize, path="/oauth/authorize", query=query
        )
        assert status == 400
        assert json.loads(body)["error"] == "invalid_request"

    async def test_token_exchange_full_flow(self, seeded_client, test_user, rsa_keys):
        """Full auth code → token exchange flow."""
        from ouvrage.auth.oauth import handle_token
        from ouvrage.crypto import decrypt_value

        # Create auth code
        code = await create_authorization_code(
            client_id="claude-mcp",
            user_id=test_user["id"],
            redirect_uri="https://claude.ai/oauth/callback",
            scope="openid profile email",
        )

        # Get client secret
        client = await get_client("claude-mcp")
        client_secret = decrypt_value(client["client_secret_encrypted"])

        # Exchange code for tokens
        body = (
            f"grant_type=authorization_code&code={code}"
            f"&client_id=claude-mcp&client_secret={client_secret}"
            f"&redirect_uri=https://claude.ai/oauth/callback"
        ).encode()

        status, _, resp_body = await self._call_handler(
            handle_token, method="POST", path="/oauth/token", body=body
        )
        assert status == 200
        data = json.loads(resp_body)
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "Bearer"

    async def test_token_exchange_with_pkce(self, seeded_client, test_user, rsa_keys):
        """Auth code flow with PKCE S256."""
        from ouvrage.auth.oauth import handle_token
        from ouvrage.crypto import decrypt_value

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
        from ouvrage.auth.oauth import handle_token
        from ouvrage.crypto import decrypt_value

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
        from ouvrage.auth.oauth import handle_token

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
        from ouvrage.auth.oauth import handle_revoke

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
        from ouvrage.auth.oauth import handle_revoke

        body = b"token=nonexistent-token"
        status, _, _ = await self._call_handler(
            handle_revoke, method="POST", path="/oauth/revoke", body=body
        )
        assert status == 200
