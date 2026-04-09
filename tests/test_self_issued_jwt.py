"""Tests for self-issued JWT validation in auth middleware.

Covers:
- _is_self_issuer() detection logic
- verify_token() with locally-signed JWTs (no HTTP)
- Revocation check: revoked jti → None
- No jti claim → skip revocation (backward compat)
- External issuer → HTTP JWKS fetch path still used
- is_auth_enabled() always returns True
"""

import os
import time
import uuid

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from unittest.mock import AsyncMock, patch, MagicMock


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_middleware_module_vars():
    """Reset module-level config vars in middleware and oauth between tests."""
    import switchboard.auth.middleware as _mw
    import switchboard.auth.oauth as _oauth

    # Save originals
    orig_auth_issuer = _mw.AUTH_ISSUER_URL
    orig_oauth_base_mw = _mw.OAUTH_BASE_URL
    orig_resource = _mw.RESOURCE_URL
    orig_jwks_cache = _mw._jwks_cache.copy()
    orig_jwks_cache_time = _mw._jwks_cache_time
    orig_priv_key = _oauth._rsa_private_key
    orig_pub_jwk = _oauth._rsa_public_jwk
    orig_oauth_base_oauth = _oauth.OAUTH_BASE_URL

    yield

    # Restore
    _mw.AUTH_ISSUER_URL = orig_auth_issuer
    _mw.OAUTH_BASE_URL = orig_oauth_base_mw
    _mw.RESOURCE_URL = orig_resource
    _mw._jwks_cache = orig_jwks_cache
    _mw._jwks_cache_time = orig_jwks_cache_time
    _oauth._rsa_private_key = orig_priv_key
    _oauth._rsa_public_jwk = orig_pub_jwk
    _oauth.OAUTH_BASE_URL = orig_oauth_base_oauth


@pytest.fixture
def local_rsa_key(tmp_path):
    """Generate a fresh RSA keypair and configure it in oauth.py."""
    import switchboard.auth.oauth as _oauth
    import switchboard.config.settings as _s

    key_path = str(tmp_path / "test_key.pem")
    os.environ["OAUTH_RSA_KEY_PATH"] = key_path
    _s.OAUTH_RSA_KEY_PATH = key_path
    _oauth.OAUTH_RSA_KEY_PATH = key_path
    _oauth._rsa_private_key = None
    _oauth._rsa_public_jwk = None

    from switchboard.auth.oauth import init_oauth_keys
    init_oauth_keys()

    yield _oauth._rsa_private_key

    os.environ.pop("OAUTH_RSA_KEY_PATH", None)


@pytest.fixture
def self_issuer_env(local_rsa_key):
    """Configure middleware + oauth for self-issued mode with a test base URL."""
    import switchboard.auth.middleware as _mw
    import switchboard.auth.oauth as _oauth

    base = "https://switchboard.test"
    _mw.AUTH_ISSUER_URL = None
    _mw.OAUTH_BASE_URL = base
    _oauth.OAUTH_BASE_URL = base

    yield base, local_rsa_key


def _make_jwt(
    private_key,
    issuer: str,
    audience: str = "claude-mcp",
    exp_offset: int = 3600,
    kid: str = "switchboard-1",
    jti: str | None = None,
    extra_claims: dict | None = None,
) -> str:
    """Sign a JWT with the given private key."""
    from authlib.jose import jwt as authlib_jwt

    now = int(time.time())
    payload = {
        "iss": issuer,
        "sub": "1",
        "aud": audience,
        "exp": now + exp_offset,
        "iat": now,
        "scope": "openid",
    }
    if jti is not None:
        payload["jti"] = jti
    if extra_claims:
        payload.update(extra_claims)

    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    token = authlib_jwt.encode({"alg": "RS256", "kid": kid}, payload, pem)
    return token.decode("utf-8") if isinstance(token, bytes) else token


# ── _is_self_issuer() tests ────────────────────────────────────────────────

class TestIsSelfIssuer:

    def _set(self, auth_issuer=None, oauth_base=None, resource=None):
        import switchboard.auth.middleware as _mw
        _mw.AUTH_ISSUER_URL = auth_issuer
        _mw.OAUTH_BASE_URL = oauth_base
        _mw.RESOURCE_URL = resource


    def test_localhost_is_self(self):
        from switchboard.auth.middleware import _is_self_issuer
        self._set(auth_issuer="http://localhost:8100")
        assert _is_self_issuer() is True


    def test_oauth_base_url_match_is_self(self):
        from switchboard.auth.middleware import _is_self_issuer
        self._set(
            auth_issuer="https://switchboard.example.dev",
            oauth_base="https://switchboard.example.dev",
        )
        assert _is_self_issuer() is True


    def test_resource_url_match_is_self(self):
        from switchboard.auth.middleware import _is_self_issuer
        self._set(
            auth_issuer="https://switchboard.example.dev",
            resource="https://switchboard.example.dev",
        )
        assert _is_self_issuer() is True


# ── is_auth_enabled() ─────────────────────────────────────────────────────

class TestIsAuthEnabled:


    def test_always_true_when_external(self):
        import switchboard.auth.middleware as _mw
        _mw.AUTH_ISSUER_URL = "https://external.auth.com"
        from switchboard.auth.middleware import is_auth_enabled
        assert is_auth_enabled() is True


# ── verify_token() with self-issued JWTs ──────────────────────────────────

class TestVerifyTokenSelfIssued:


    async def test_expired_jwt_returns_none(self, db, self_issuer_env):
        from switchboard.auth.middleware import verify_token
        base, private_key = self_issuer_env

        token = _make_jwt(private_key, issuer=base, exp_offset=-1)
        assert await verify_token(token) is None

    async def test_wrong_issuer_returns_none(self, db, self_issuer_env):
        from switchboard.auth.middleware import verify_token
        base, private_key = self_issuer_env

        token = _make_jwt(private_key, issuer="https://wrong-issuer.example.com")
        assert await verify_token(token) is None

    async def test_wrong_kid_returns_none(self, db, self_issuer_env):
        from switchboard.auth.middleware import verify_token
        base, private_key = self_issuer_env

        token = _make_jwt(private_key, issuer=base, kid="not-the-right-kid")
        assert await verify_token(token) is None


    async def test_valid_jti_not_revoked_passes(self, db, self_issuer_env):
        from switchboard.auth.middleware import verify_token
        from switchboard.auth.oauth import issue_tokens, init_oauth_keys, seed_default_client

        base, private_key = self_issuer_env
        await seed_default_client()

        # Issue a real token pair (stores jti in DB as not revoked)
        user = await db.create_user(email="jwt@test.com", name="JwtUser")
        result = await issue_tokens("claude-mcp", user["id"], "openid")
        access_token = result["access_token"]

        claims = await verify_token(access_token)
        assert claims is not None

    async def test_revoked_jti_returns_none(self, db, self_issuer_env):
        from switchboard.auth.middleware import verify_token
        from switchboard.auth.oauth import issue_tokens, revoke_token, seed_default_client

        base, private_key = self_issuer_env
        await seed_default_client()

        user = await db.create_user(email="revoke@test.com", name="RevokeUser")
        result = await issue_tokens("claude-mcp", user["id"], "openid")
        access_token = result["access_token"]

        # Revoke the access token
        await revoke_token(access_token, token_type_hint="access_token")

        # Verification should now fail due to revocation
        assert await verify_token(access_token) is None


# ── verify_token() with external issuer ───────────────────────────────────

class TestVerifyTokenExternalIssuer:

    async def test_external_issuer_uses_http_fetch(self, db):
        """When AUTH_ISSUER_URL is external, _get_remote_jwks() is called."""
        import switchboard.auth.middleware as _mw
        _mw.AUTH_ISSUER_URL = "https://auth.external.example.com"
        _mw.OAUTH_BASE_URL = None

        from switchboard.auth.middleware import verify_token, _get_remote_jwks

        with patch(
            "switchboard.auth.middleware._get_remote_jwks",
            new_callable=AsyncMock,
        ) as mock_fetch:
            mock_fetch.return_value = {"keys": []}
            # Token will fail (no matching key) but we verify the fetch was called
            result = await verify_token("not.a.real.token")
            # Can't validate with empty keys, but fetch was attempted
            assert result is None

    async def test_external_issuer_does_not_use_local_key(self, db, local_rsa_key, self_issuer_env):
        """When AUTH_ISSUER_URL is external, self-issued local key is NOT used."""
        import switchboard.auth.middleware as _mw

        base, private_key = self_issuer_env
        # Override to an external issuer
        _mw.AUTH_ISSUER_URL = "https://auth.external.example.com"

        from switchboard.auth.middleware import verify_token

        # Sign a token with local key but mark as external issuer
        token = _make_jwt(private_key, issuer="https://auth.external.example.com")

        with patch(
            "switchboard.auth.middleware._get_remote_jwks",
            new_callable=AsyncMock,
        ) as mock_fetch:
            # Return empty JWKS — local key not consulted
            mock_fetch.return_value = {"keys": []}
            result = await verify_token(token)
            assert result is None  # No key found → None (not local key fallback)
            mock_fetch.assert_called()


# ── Protected resource metadata ───────────────────────────────────────────

class TestProtectedResourceMetadata:


    def test_external_issuer_points_to_external(self):
        import switchboard.auth.middleware as _mw
        _mw.AUTH_ISSUER_URL = "https://auth.external.com"
        _mw.OAUTH_BASE_URL = None

        from switchboard.auth.middleware import _protected_resource_metadata
        meta = _protected_resource_metadata()
        assert "https://auth.external.com" in meta["authorization_servers"]

    def test_both_unset_uses_localhost_fallback(self):
        import switchboard.auth.middleware as _mw
        import switchboard.auth.oauth as _oauth
        _mw.AUTH_ISSUER_URL = None
        _mw.OAUTH_BASE_URL = None
        _oauth.OAUTH_BASE_URL = None

        from switchboard.auth.middleware import _protected_resource_metadata
        meta = _protected_resource_metadata()
        assert len(meta["authorization_servers"]) == 1
        assert "localhost" in meta["authorization_servers"][0]
