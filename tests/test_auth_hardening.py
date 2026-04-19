"""Tests for auth hardening fixes.

Covers:
- Localhost bypass scoped to /mcp/worker, /proxy/anthropic, /health only
- /mcp and /dashboard/api from localhost are NOT bypassed
- secrets.compare_digest used in internal/api.py
- Fail-closed revocation check
"""

import inspect
import json
import secrets as secrets_module
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from ouvrage.auth.middleware import auth_middleware


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_scope(
    path: str,
    method: str = "GET",
    client: tuple = ("10.0.0.1", 12345),
    headers: list | None = None,
) -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": headers or [],
        "client": client,
    }


_LOCALHOST = ("127.0.0.1", 54321)
_IPV6_LOCALHOST = ("::1", 54321)
_REMOTE = ("10.0.0.1", 12345)


async def _call(path, client=_REMOTE, method="GET", headers=None):
    """Call auth_middleware and return (status, headers_dict, body_bytes, inner_reached)."""
    inner_called = []

    async def inner_app(scope, receive, send):
        inner_called.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"OK"})

    app = auth_middleware(inner_app)
    scope = _make_scope(path, method=method, client=client, headers=headers)

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
                resp_headers[key.lower()] = val
        elif message["type"] == "http.response.body":
            body += message.get("body", b"")

    await app(scope, receive, send)
    return status, resp_headers, body, bool(inner_called)


# ── Localhost bypass scoping ────────────────────────────────────────────────

class TestLocalhostBypass:

    async def test_localhost_mcp_worker_allowed(self, db):
        """/mcp/worker from localhost bypasses auth and reaches inner app."""
        status, _, _, reached = await _call("/mcp/worker", client=_LOCALHOST)
        assert status == 200
        assert reached

    async def test_localhost_ipv6_mcp_worker_allowed(self, db):
        """::1 (IPv6 localhost) also bypasses auth for /mcp/worker."""
        status, _, _, reached = await _call("/mcp/worker", client=_IPV6_LOCALHOST)
        assert status == 200
        assert reached

    async def test_localhost_proxy_anthropic_allowed(self, db):
        """/proxy/anthropic/... from localhost bypasses auth."""
        status, _, _, reached = await _call(
            "/proxy/anthropic/1/v1/messages", client=_LOCALHOST
        )
        assert status == 200
        assert reached

    async def test_localhost_health_allowed(self, db):
        """/health from localhost bypasses auth."""
        status, _, _, reached = await _call("/health", client=_LOCALHOST)
        assert status == 200
        assert reached

    async def test_localhost_mcp_not_bypassed(self, db):
        """/mcp from localhost is NOT in bypass allowlist — goes through JWT auth."""
        # No Bearer token → 401
        status, _, _, reached = await _call("/mcp", client=_LOCALHOST)
        assert status == 401
        assert not reached

    async def test_localhost_dashboard_api_not_bypassed(self, db):
        """/dashboard/api/* from localhost is NOT bypassed — session auth applies."""
        status, _, body, reached = await _call(
            "/dashboard/api/tasks", client=_LOCALHOST
        )
        # No session cookie → 401 authentication_required
        assert status == 401
        data = json.loads(body)
        assert data["error"] == "authentication_required"
        assert not reached

    async def test_localhost_dashboard_not_bypassed(self, db):
        """/dashboard from localhost is NOT bypassed — session auth applies."""
        status, headers, _, reached = await _call("/dashboard", client=_LOCALHOST)
        # No session → 302 redirect to login
        assert status == 302
        assert not reached

    async def test_remote_mcp_worker_needs_auth(self):
        """/mcp/worker from non-localhost requires Bearer JWT."""
        status, _, _, reached = await _call("/mcp/worker", client=_REMOTE)
        # No token → 401
        assert status == 401
        assert not reached

    async def test_remote_proxy_anthropic_redirected(self):
        """/proxy/anthropic from non-localhost is not in /mcp or /mcp/worker → redirected."""
        status, headers, _, reached = await _call(
            "/proxy/anthropic/1/v1/messages", client=_REMOTE
        )
        # Redirected to /dashboard (unknown path, not /mcp or /mcp/worker)
        assert status == 302
        assert not reached


# ── secrets.compare_digest ──────────────────────────────────────────────────

class TestCompareDigest:

    def test_internal_api_source_uses_compare_digest(self):
        """_check_auth source must use secrets.compare_digest, not == comparison."""
        from ouvrage.internal import api as internal_api
        source = inspect.getsource(internal_api._check_auth)
        assert "secrets.compare_digest" in source
        # Must NOT use plain equality for token comparison
        assert "token ==" not in source

    async def test_compare_digest_called_on_valid_token(self):
        """secrets.compare_digest is invoked during auth check with correct token."""
        from ouvrage.internal import api as internal_api
        scope = {
            "headers": [(b"authorization", b"Bearer mysecret")],
        }
        with patch("ouvrage.internal.api.INTERNAL_API_TOKEN", "mysecret"):
            with patch(
                "ouvrage.internal.api.secrets.compare_digest",
                wraps=secrets_module.compare_digest,
            ) as mock_cd:
                result = internal_api._check_auth(scope)
                assert result is True
                mock_cd.assert_called_once_with("mysecret", "mysecret")

    async def test_compare_digest_called_on_invalid_token(self):
        """secrets.compare_digest is invoked even when token is wrong (timing-safe)."""
        from ouvrage.internal import api as internal_api
        scope = {
            "headers": [(b"authorization", b"Bearer wrongtoken")],
        }
        with patch("ouvrage.internal.api.INTERNAL_API_TOKEN", "correcttoken"):
            with patch(
                "ouvrage.internal.api.secrets.compare_digest",
                wraps=secrets_module.compare_digest,
            ) as mock_cd:
                result = internal_api._check_auth(scope)
                assert result is False
                mock_cd.assert_called_once()


# ── Fail-closed revocation ──────────────────────────────────────────────────

class TestFailClosedRevocation:

    async def test_revocation_check_fails_closed_on_db_error(self):
        """_is_token_revoked returns True (token IS revoked) when DB raises."""
        from ouvrage.auth import middleware

        # Simulate a DB connection manager that raises on __aenter__
        bad_cm = MagicMock()
        bad_cm.__aenter__ = AsyncMock(side_effect=RuntimeError("DB down"))
        bad_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("ouvrage.db.connection.get_db", return_value=bad_cm):
            result = await middleware._is_token_revoked("test-jti")

        assert result is True, "DB error must fail closed (return True = revoked)"

    async def test_revocation_check_returns_false_for_valid_token(self, db):
        """_is_token_revoked returns False for a JTI not in the revoked table."""
        from ouvrage.auth import middleware
        # JTI not in table at all → not revoked
        result = await middleware._is_token_revoked("nonexistent-jti")
        assert result is False
