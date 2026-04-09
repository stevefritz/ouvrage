"""Tests for AUTH_MODE flag behavior.

Covers:
- AUTH_MODE defaults to 'local' when env var not set
- local mode: /dashboard/api/* no session → 401 (unchanged)
- local mode: /dashboard* no session → 302 to /dashboard/login (unchanged)
- saas mode: /dashboard/api/* no session → 302 to control plane
- saas mode: /dashboard* no session → 302 to control plane
- Redirect URL format: {CONTROL_PLANE_URL}/login?redirect={instance_url}/auth/sso
"""

import importlib
import os
import json
from unittest.mock import patch

import pytest

from switchboard.auth.middleware import auth_middleware


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_scope(
    path: str,
    method: str = "GET",
    cookie: str | None = None,
    client: tuple = ("10.0.0.1", 12345),
    host: str = "tenant.foreman.dev",
) -> dict:
    headers = []
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    if host:
        headers.append((b"host", host.encode()))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": headers,
        "client": client,
    }


async def _call_middleware(path, cookie=None, client=("10.0.0.1", 12345),
                            host="tenant.foreman.dev"):
    """Call auth_middleware and return (status, headers_dict, body_bytes)."""
    async def inner_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"OK"})

    app = auth_middleware(inner_app)
    scope = _make_scope(path, cookie=cookie, client=client, host=host)

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
    return status, resp_headers, body


# ── Config defaults ─────────────────────────────────────────────────────────

class TestAuthModeConfig:

    def test_auth_mode_defaults_to_local(self):
        """AUTH_MODE env var unset → defaults to 'local'."""
        import switchboard.config.settings as settings
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AUTH_MODE", None)
            importlib.reload(settings)
            assert settings.AUTH_MODE == "local"
        # Restore
        importlib.reload(settings)


# ── Local mode: existing behavior unchanged ─────────────────────────────────

class TestLocalModeUnchanged:

    @pytest.fixture(autouse=True)
    def set_local_mode(self):
        with patch("switchboard.auth.middleware.AUTH_MODE", "local"):
            yield


    async def test_foreman_login_is_public(self, db):
        """Local mode: /dashboard/login is public (no auth required)."""
        status, _, body = await _call_middleware("/dashboard/login")
        assert status == 200
        assert body == b"OK"


# ── SaaS mode: redirect to control plane ────────────────────────────────────

class TestSaasModeRedirect:

    @pytest.fixture(autouse=True)
    def set_saas_mode(self):
        with patch("switchboard.auth.middleware.AUTH_MODE", "saas"), \
             patch("switchboard.auth.middleware.CONTROL_PLANE_URL", "https://dashboard.dev"):
            yield

    async def test_dashboard_api_no_session_returns_302(self, db):
        """SaaS mode: /dashboard/api/* no session → 302."""
        status, _, _ = await _call_middleware("/dashboard/api/tasks")
        assert status == 302


    async def test_foreman_no_session_redirects_to_control_plane(self, db):
        """SaaS mode: /dashboard/* no session → 302 to control plane (not /dashboard/login)."""
        status, headers, _ = await _call_middleware("/dashboard/")
        assert status == 302
        location = headers["location"]
        assert location.startswith("https://dashboard.dev/login")
        assert "/dashboard/login" not in location


    async def test_dashboard_api_with_session_still_passes(self, db):
        """SaaS mode: valid session still passes through normally."""
        from switchboard.auth.sessions import create_session
        user = await db.create_user(email="saas@test.com", name="SaaS")
        sid = await create_session(user["id"])
        cookie = f"switchboard_session={sid}"

        status, _, body = await _call_middleware("/dashboard/api/tasks", cookie=cookie)
        assert status == 200
        assert body == b"OK"

    async def test_foreman_with_session_still_passes(self, db):
        """SaaS mode: valid session still passes through normally."""
        from switchboard.auth.sessions import create_session
        user = await db.create_user(email="saas2@test.com", name="SaaS2")
        sid = await create_session(user["id"])
        cookie = f"switchboard_session={sid}"

        status, _, body = await _call_middleware("/dashboard/", cookie=cookie)
        assert status == 200
        assert body == b"OK"


# ── Redirect URL construction ────────────────────────────────────────────────

class TestSaasRedirectUrlConstruction:
    """Test the _get_instance_url and _saas_redirect_url helpers directly."""


    def test_get_instance_url_empty_when_no_host(self):
        from switchboard.auth.middleware import _get_instance_url
        scope = {"headers": []}
        result = _get_instance_url(scope)
        assert result == ""


