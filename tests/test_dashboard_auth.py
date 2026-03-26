"""Tests for session-based auth protection of /foreman* and /dashboard/api/*.

Covers:
- /foreman/* without session → 302 redirect to /foreman/login?next=...
- /foreman/* with session → passes through (200)
- /foreman/login → public, no redirect
- /dashboard/api/* without session → 401 JSON {"error": "authentication_required"}
- /dashboard/api/* with session → passes through, session_user injected into scope
- /dashboard/ static → passes without session
- localhost bypasses all session auth
"""

import json
import pytest

from switchboard.auth.middleware import auth_middleware


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_scope(
    path: str,
    method: str = "GET",
    cookie: str | None = None,
    client: tuple = ("10.0.0.1", 12345),  # non-localhost by default
    query_string: bytes = b"",
) -> dict:
    headers = []
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string,
        "headers": headers,
        "client": client,
    }


async def _call_middleware(path, cookie=None, client=("10.0.0.1", 12345),
                            method="GET", query_string=b""):
    """Call auth_middleware wrapping a simple pass-through app.

    Returns (status, resp_headers, body_bytes, inner_scope_snapshot).
    inner_scope_snapshot is the scope dict seen by inner_app (if called).
    """
    inner_called = []
    inner_scope_ref = []

    async def inner_app(scope, receive, send):
        inner_called.append(True)
        inner_scope_ref.append(dict(scope))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"OK"})

    app = auth_middleware(inner_app)

    scope = _make_scope(path, method=method, cookie=cookie, client=client,
                        query_string=query_string)

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

    inner_scope = inner_scope_ref[0] if inner_scope_ref else None
    return status, resp_headers, body, inner_scope


# ── Foreman: no session → redirect ─────────────────────────────────────────

class TestForemanSessionRequired:

    async def test_foreman_root_no_session_redirects(self, db):
        status, headers, _, _ = await _call_middleware("/foreman/")
        assert status == 302
        assert headers["location"].startswith("/foreman/login?next=")

    async def test_foreman_nested_no_session_redirects(self, db):
        status, headers, _, _ = await _call_middleware("/foreman/tasks")
        assert status == 302
        assert "/foreman/login?next=" in headers["location"]

    async def test_foreman_redirect_encodes_next_path(self, db):
        status, headers, _, _ = await _call_middleware("/foreman/tasks")
        location = headers["location"]
        assert "next=" in location
        # next= should contain the original path (URL-encoded)
        assert "foreman" in location

    async def test_foreman_redirect_includes_query_string(self, db):
        status, headers, _, _ = await _call_middleware(
            "/foreman/tasks", query_string=b"status=working"
        )
        location = headers["location"]
        # The query string should be included in next=
        assert "status" in location or "working" in location

    async def test_foreman_login_is_public(self, db):
        """GET /foreman/login should pass through without session."""
        status, _, body, _ = await _call_middleware("/foreman/login")
        assert status == 200
        assert body == b"OK"

    async def test_foreman_with_valid_session_passes(self, db):
        from switchboard.auth.sessions import create_session
        user = await db.create_user(email="fore@test.com", name="Fore")
        sid = await create_session(user["id"])
        cookie = f"switchboard_session={sid}"

        status, _, body, _ = await _call_middleware("/foreman/", cookie=cookie)
        assert status == 200
        assert body == b"OK"

    async def test_foreman_with_session_injects_user(self, db):
        from switchboard.auth.sessions import create_session
        user = await db.create_user(email="fore2@test.com", name="Fore2")
        sid = await create_session(user["id"])
        cookie = f"switchboard_session={sid}"

        _, _, _, inner_scope = await _call_middleware("/foreman/", cookie=cookie)
        assert inner_scope is not None
        assert "session_user" in inner_scope
        assert inner_scope["session_user"]["id"] == user["id"]
        assert inner_scope["session_user"]["email"] == "fore2@test.com"


# ── Dashboard API: no session → 401 ────────────────────────────────────────

class TestDashboardApiSessionRequired:

    async def test_dashboard_api_no_session_returns_401(self, db):
        status, _, body, _ = await _call_middleware("/dashboard/api/tasks")
        assert status == 401
        data = json.loads(body)
        assert data["error"] == "authentication_required"

    async def test_dashboard_api_nested_no_session_returns_401(self, db):
        status, _, body, _ = await _call_middleware("/dashboard/api/projects/foo/tasks")
        assert status == 401

    async def test_dashboard_api_with_valid_session_passes(self, db):
        from switchboard.auth.sessions import create_session
        user = await db.create_user(email="dash@test.com", name="Dash")
        sid = await create_session(user["id"])
        cookie = f"switchboard_session={sid}"

        status, _, body, _ = await _call_middleware("/dashboard/api/tasks", cookie=cookie)
        assert status == 200
        assert body == b"OK"

    async def test_dashboard_api_with_session_injects_user(self, db):
        from switchboard.auth.sessions import create_session
        user = await db.create_user(email="dash2@test.com", name="Dash2")
        sid = await create_session(user["id"])
        cookie = f"switchboard_session={sid}"

        _, _, _, inner_scope = await _call_middleware("/dashboard/api/tasks", cookie=cookie)
        assert inner_scope is not None
        assert "session_user" in inner_scope
        assert inner_scope["session_user"]["id"] == user["id"]

    async def test_dashboard_api_401_is_json(self, db):
        """401 response must be JSON, not HTML."""
        _, headers, body, _ = await _call_middleware("/dashboard/api/tasks")
        # Body should parse as JSON
        data = json.loads(body)
        assert "error" in data


# ── Dashboard static: no auth needed ───────────────────────────────────────

class TestDashboardStaticNoAuth:

    async def test_dashboard_root_passes_without_session(self, db):
        status, _, body, _ = await _call_middleware("/dashboard")
        assert status == 200

    async def test_dashboard_html_passes_without_session(self, db):
        status, _, _, _ = await _call_middleware("/dashboard/index.html")
        assert status == 200

    async def test_dashboard_js_passes_without_session(self, db):
        status, _, _, _ = await _call_middleware("/dashboard/app.js")
        assert status == 200


# ── Localhost bypass ────────────────────────────────────────────────────────

class TestLocalhostBypass:

    async def test_localhost_bypasses_foreman_auth(self, db):
        """127.0.0.1 skips session check on /foreman/."""
        status, _, body, _ = await _call_middleware(
            "/foreman/", client=("127.0.0.1", 5000)
        )
        assert status == 200
        assert body == b"OK"

    async def test_localhost_ipv6_bypasses_foreman_auth(self, db):
        status, _, body, _ = await _call_middleware(
            "/foreman/", client=("::1", 5000)
        )
        assert status == 200

    async def test_localhost_bypasses_dashboard_api_auth(self, db):
        """127.0.0.1 skips session check on /dashboard/api/."""
        status, _, body, _ = await _call_middleware(
            "/dashboard/api/tasks", client=("127.0.0.1", 5000)
        )
        assert status == 200
        assert body == b"OK"
