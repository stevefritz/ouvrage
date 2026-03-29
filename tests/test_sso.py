"""Tests for the /auth/sso endpoint.

Covers:
- Valid JWT → session created, redirect to /foreman
- Expired JWT → 401
- Wrong audience → 401
- Tampered signature → 401
- Missing token param → 400
- AUTH_MODE=local → 404
- Redirect param preserved through flow
- First SSO for email → new user created in DB
- Second SSO for same email → existing user reused
- Role update when JWT role differs from stored role
"""

import json
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

from switchboard.auth.sso import handle_sso, get_jwks, _invalidate_jwks_cache


# ── Key generation helpers ──────────────────────────────────────────────────

def _generate_rsa_key():
    """Generate an RSA private key for testing."""
    return rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )


def _private_key_to_jwk(private_key, kid: str = "test-key-1") -> dict:
    """Build a JWK dict (public key portion) from an RSA private key."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    import base64
    import struct

    pub = private_key.public_key()
    pub_numbers = pub.public_key().public_numbers() if hasattr(pub, 'public_key') else pub.public_numbers()

    def _int_to_base64url(n: int) -> str:
        length = (n.bit_length() + 7) // 8
        return base64.urlsafe_b64encode(n.to_bytes(length, 'big')).rstrip(b'=').decode()

    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _int_to_base64url(pub_numbers.n),
        "e": _int_to_base64url(pub_numbers.e),
    }


def _build_jwks(private_key, kid: str = "test-key-1") -> dict:
    """Build JWKS dict from an RSA private key."""
    return {"keys": [_private_key_to_jwk(private_key, kid)]}


def _make_jwt(
    private_key,
    kid: str = "test-key-1",
    audience: str = "test-instance",
    email: str = "user@example.com",
    role: str = "member",
    sub: str = "cust_123",
    slug: str = "test-tenant",
    exp_offset: int = 3600,  # seconds from now; negative = expired
) -> str:
    """Sign and return a JWT using the given RSA private key."""
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption
    )
    now = int(time.time())
    payload = {
        "sub": sub,
        "email": email,
        "slug": slug,
        "role": role,
        "aud": audience,
        "iat": now,
        "exp": now + exp_offset,
    }
    return jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
        headers={"kid": kid},
    )


# ── ASGI test helpers ───────────────────────────────────────────────────────

def _make_scope(
    token: str | None = None,
    redirect: str | None = None,
    client: tuple = ("10.0.0.1", 12345),
) -> dict:
    qs_parts = []
    if token is not None:
        from urllib.parse import quote
        qs_parts.append(f"token={quote(token)}")
    if redirect is not None:
        from urllib.parse import quote
        qs_parts.append(f"redirect={quote(redirect)}")
    qs = "&".join(qs_parts).encode()
    return {
        "type": "http",
        "method": "GET",
        "path": "/auth/sso",
        "query_string": qs,
        "headers": [],
        "client": client,
    }


async def _call_sso(
    token: str | None = None,
    redirect: str | None = None,
    client: tuple = ("10.0.0.1", 12345),
):
    """Call handle_sso and return (status, headers_dict, body_bytes)."""
    scope = _make_scope(token=token, redirect=redirect, client=client)
    status = None
    resp_headers = {}
    body = b""

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        nonlocal status, body
        if message["type"] == "http.response.start":
            status = message["status"]
            for k, v in message.get("headers", []):
                key = k.decode() if isinstance(k, bytes) else k
                val = v.decode() if isinstance(v, bytes) else v
                # collect-append for duplicate headers (e.g. multiple set-cookie)
                lower_key = key.lower()
                if lower_key in resp_headers:
                    if isinstance(resp_headers[lower_key], list):
                        resp_headers[lower_key].append(val)
                    else:
                        resp_headers[lower_key] = [resp_headers[lower_key], val]
                else:
                    resp_headers[lower_key] = val
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    await handle_sso(scope, receive, send)
    return status, resp_headers, body


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_jwks_cache():
    """Reset the JWKS cache before each test."""
    _invalidate_jwks_cache()
    import switchboard.auth.sso as _sso
    _sso._jwks_cache = None
    yield
    _invalidate_jwks_cache()
    _sso._jwks_cache = None


# ── Tests: AUTH_MODE=local ───────────────────────────────────────────────────

class TestLocalMode:

    @pytest.fixture(autouse=True)
    def local_mode(self):
        with patch("switchboard.auth.sso.AUTH_MODE", "local"):
            yield

    async def test_returns_404_in_local_mode(self, db):
        """AUTH_MODE=local → 404 regardless of token."""
        status, _, _ = await _call_sso(token="whatever")
        assert status == 404

    async def test_returns_404_without_token_in_local_mode(self, db):
        """AUTH_MODE=local → 404 even without token param."""
        status, _, _ = await _call_sso()
        assert status == 404


# ── Tests: missing/bad token ─────────────────────────────────────────────────

class TestTokenValidation:

    @pytest.fixture(autouse=True)
    def saas_mode(self):
        with patch("switchboard.auth.sso.AUTH_MODE", "saas"), \
             patch("switchboard.auth.sso.CONTROL_PLANE_JWKS", "https://cp.example.com/.well-known/jwks.json"), \
             patch("switchboard.auth.sso.INSTANCE_SLUG", "test-instance"):
            yield

    async def test_missing_token_returns_400(self, db):
        """No ?token= param → 400."""
        status, _, body = await _call_sso()
        assert status == 400
        data = json.loads(body)
        assert data["error"] == "missing_token"

    async def test_expired_jwt_returns_401(self, db):
        """Expired JWT → 401."""
        key = _generate_rsa_key()
        jwks = _build_jwks(key)
        token = _make_jwt(key, audience="test-instance", exp_offset=-100)

        with patch("switchboard.auth.sso._fetch_jwks", AsyncMock(return_value=jwks)):
            status, _, body = await _call_sso(token=token)

        assert status == 401
        data = json.loads(body)
        assert data["error"] == "invalid_token"

    async def test_wrong_audience_returns_401(self, db):
        """JWT with wrong audience → 401."""
        key = _generate_rsa_key()
        jwks = _build_jwks(key)
        token = _make_jwt(key, audience="wrong-audience")

        with patch("switchboard.auth.sso._fetch_jwks", AsyncMock(return_value=jwks)):
            status, _, body = await _call_sso(token=token)

        assert status == 401
        data = json.loads(body)
        assert data["error"] == "invalid_token"

    async def test_tampered_signature_returns_401(self, db):
        """JWT signed with different key → 401."""
        signing_key = _generate_rsa_key()
        different_key = _generate_rsa_key()
        # JWKS has signing_key's public key, but token is signed with different_key
        jwks = _build_jwks(signing_key)
        token = _make_jwt(different_key, audience="test-instance")

        with patch("switchboard.auth.sso._fetch_jwks", AsyncMock(return_value=jwks)):
            status, _, body = await _call_sso(token=token)

        assert status == 401
        data = json.loads(body)
        assert data["error"] == "invalid_token"

    async def test_malformed_jwt_returns_401(self, db):
        """Completely invalid token string → 401."""
        with patch("switchboard.auth.sso._fetch_jwks", AsyncMock(return_value={"keys": []})):
            status, _, body = await _call_sso(token="not.a.jwt")

        assert status == 401


# ── Tests: valid JWT flow ─────────────────────────────────────────────────────

class TestValidJwtFlow:

    @pytest.fixture(autouse=True)
    def saas_mode(self):
        with patch("switchboard.auth.sso.AUTH_MODE", "saas"), \
             patch("switchboard.auth.sso.CONTROL_PLANE_JWKS", "https://cp.example.com/.well-known/jwks.json"), \
             patch("switchboard.auth.sso.INSTANCE_SLUG", "test-instance"):
            yield

    async def test_valid_jwt_creates_session_and_redirects(self, db):
        """Valid JWT → session cookie set, 302 to /foreman."""
        key = _generate_rsa_key()
        jwks = _build_jwks(key)
        token = _make_jwt(key, audience="test-instance", email="user@example.com")

        with patch("switchboard.auth.sso._fetch_jwks", AsyncMock(return_value=jwks)):
            status, headers, _ = await _call_sso(token=token)

        assert status == 302
        assert headers["location"] == "/foreman"
        assert "set-cookie" in headers
        cookie = headers["set-cookie"] if isinstance(headers["set-cookie"], str) else headers["set-cookie"][0]
        assert "switchboard_session=" in cookie

    async def test_valid_jwt_redirects_to_redirect_param(self, db):
        """Valid JWT with redirect param → redirects to that path."""
        key = _generate_rsa_key()
        jwks = _build_jwks(key)
        token = _make_jwt(key, audience="test-instance", email="user@example.com")

        with patch("switchboard.auth.sso._fetch_jwks", AsyncMock(return_value=jwks)):
            status, headers, _ = await _call_sso(token=token, redirect="/foreman/tasks")

        assert status == 302
        assert headers["location"] == "/foreman/tasks"

    async def test_redirect_absolute_url_falls_back_to_foreman(self, db):
        """Absolute redirect URL → rejected, falls back to /foreman."""
        key = _generate_rsa_key()
        jwks = _build_jwks(key)
        token = _make_jwt(key, audience="test-instance", email="user@example.com")

        with patch("switchboard.auth.sso._fetch_jwks", AsyncMock(return_value=jwks)):
            status, headers, _ = await _call_sso(
                token=token, redirect="https://evil.com/steal"
            )

        assert status == 302
        assert headers["location"] == "/foreman"


# ── Tests: user upsert ────────────────────────────────────────────────────────

class TestUserUpsert:

    @pytest.fixture(autouse=True)
    def saas_mode(self):
        with patch("switchboard.auth.sso.AUTH_MODE", "saas"), \
             patch("switchboard.auth.sso.CONTROL_PLANE_JWKS", "https://cp.example.com/.well-known/jwks.json"), \
             patch("switchboard.auth.sso.INSTANCE_SLUG", "test-instance"):
            yield

    async def test_first_sso_creates_user(self, db):
        """First SSO for an email → new user created in DB."""
        key = _generate_rsa_key()
        jwks = _build_jwks(key)
        token = _make_jwt(key, audience="test-instance", email="newuser@example.com", role="admin")

        # Confirm user does not exist yet
        from switchboard.db.users import get_user_by_email
        assert await get_user_by_email("newuser@example.com") is None

        with patch("switchboard.auth.sso._fetch_jwks", AsyncMock(return_value=jwks)):
            status, _, _ = await _call_sso(token=token)

        assert status == 302
        user = await get_user_by_email("newuser@example.com")
        assert user is not None
        assert user["email"] == "newuser@example.com"
        assert user["role"] == "admin"

    async def test_second_sso_reuses_existing_user(self, db):
        """Second SSO for same email → user ID is unchanged."""
        from switchboard.db.users import get_user_by_email
        key = _generate_rsa_key()
        jwks = _build_jwks(key)
        token = _make_jwt(key, audience="test-instance", email="existing@example.com")

        # First SSO
        with patch("switchboard.auth.sso._fetch_jwks", AsyncMock(return_value=jwks)):
            await _call_sso(token=token)

        user_after_first = await get_user_by_email("existing@example.com")
        first_id = user_after_first["id"]

        # Second SSO — regenerate token but same email
        token2 = _make_jwt(key, audience="test-instance", email="existing@example.com")
        with patch("switchboard.auth.sso._fetch_jwks", AsyncMock(return_value=jwks)):
            await _call_sso(token=token2)

        user_after_second = await get_user_by_email("existing@example.com")
        assert user_after_second["id"] == first_id  # same user, not duplicated

    async def test_role_update_on_subsequent_sso(self, db):
        """Subsequent SSO with changed role → role is updated."""
        from switchboard.db.users import get_user_by_email
        key = _generate_rsa_key()
        jwks = _build_jwks(key)

        # First SSO with role=member
        token1 = _make_jwt(key, audience="test-instance", email="role@example.com", role="member")
        with patch("switchboard.auth.sso._fetch_jwks", AsyncMock(return_value=jwks)):
            await _call_sso(token=token1)

        user = await get_user_by_email("role@example.com")
        assert user["role"] == "member"

        # Second SSO with role=admin
        token2 = _make_jwt(key, audience="test-instance", email="role@example.com", role="admin")
        with patch("switchboard.auth.sso._fetch_jwks", AsyncMock(return_value=jwks)):
            await _call_sso(token=token2)

        user_updated = await get_user_by_email("role@example.com")
        assert user_updated["role"] == "admin"

    async def test_email_is_lowercased(self, db):
        """JWT email is normalized to lowercase before upsert."""
        from switchboard.db.users import get_user_by_email
        key = _generate_rsa_key()
        jwks = _build_jwks(key)
        token = _make_jwt(key, audience="test-instance", email="User@Example.COM")

        with patch("switchboard.auth.sso._fetch_jwks", AsyncMock(return_value=jwks)):
            await _call_sso(token=token)

        user = await get_user_by_email("user@example.com")
        assert user is not None


# ── Tests: JWKS caching ───────────────────────────────────────────────────────

class TestJwksCaching:

    @pytest.fixture(autouse=True)
    def saas_mode(self):
        with patch("switchboard.auth.sso.AUTH_MODE", "saas"), \
             patch("switchboard.auth.sso.CONTROL_PLANE_JWKS", "https://cp.example.com/.well-known/jwks.json"), \
             patch("switchboard.auth.sso.INSTANCE_SLUG", "test-instance"):
            yield

    async def test_jwks_fetched_only_once_per_ttl(self, db):
        """JWKS is fetched once and cached for subsequent requests."""
        key = _generate_rsa_key()
        jwks = _build_jwks(key)
        fetch_mock = AsyncMock(return_value=jwks)

        with patch("switchboard.auth.sso._fetch_jwks", fetch_mock):
            token1 = _make_jwt(key, audience="test-instance", email="a@example.com")
            token2 = _make_jwt(key, audience="test-instance", email="b@example.com")
            await _call_sso(token=token1)
            await _call_sso(token=token2)

        # Should have fetched JWKS only once (cache hit on second call)
        assert fetch_mock.call_count == 1

    async def test_jwks_cache_refreshed_when_stale(self, db):
        """Stale cache (past TTL) → JWKS re-fetched."""
        import switchboard.auth.sso as sso_mod
        key = _generate_rsa_key()
        jwks = _build_jwks(key)
        fetch_mock = AsyncMock(return_value=jwks)

        with patch("switchboard.auth.sso._fetch_jwks", fetch_mock):
            # First request
            token = _make_jwt(key, audience="test-instance", email="c@example.com")
            await _call_sso(token=token)

            # Expire the cache
            sso_mod._jwks_fetched_at = time.time() - 7200  # 2 hours ago

            token2 = _make_jwt(key, audience="test-instance", email="d@example.com")
            await _call_sso(token=token2)

        assert fetch_mock.call_count == 2

    async def test_signature_failure_invalidates_cache_and_retries(self, db):
        """kid not in cached JWKS → cache refreshed, key searched again."""
        key = _generate_rsa_key()
        jwks = _build_jwks(key, kid="test-key-1")
        fetch_mock = AsyncMock(return_value=jwks)

        # Token signed with "test-key-1" but initial cache is empty
        import switchboard.auth.sso as sso_mod
        sso_mod._jwks_cache = {"keys": []}  # stale/empty cache
        sso_mod._jwks_fetched_at = time.time()  # mark as fresh so TTL won't force refresh

        token = _make_jwt(key, kid="test-key-1", audience="test-instance", email="e@example.com")

        with patch("switchboard.auth.sso._fetch_jwks", fetch_mock):
            status, _, _ = await _call_sso(token=token)

        # Should have fetched JWKS once (forced refresh due to kid miss)
        assert fetch_mock.call_count == 1
        assert status == 302  # succeeded after retry
