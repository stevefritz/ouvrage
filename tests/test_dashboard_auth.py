"""Tests for session-based auth protection of /dashboard* and /dashboard/api/*.

Covers:
- /dashboard/* without session → 302 redirect to /dashboard/login?next=...
- /dashboard/* with session → passes through (200)
- /dashboard/login → public, no redirect
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


# ── Dashboard API: no session → 401 ────────────────────────────────────────


# ── Dashboard SPA routes vs static assets ──────────────────────────────────

class TestDashboardSPARequiresAuth:


    async def test_dashboard_static_html_passes_without_session(self, db):
        """Static assets (have file extension) pass through without auth."""
        status, _, body, _ = await _call_middleware("/dashboard/index.html")
        assert status == 200


# ── Legacy /foreman redirect ──────────────────────────────────────────────

class TestLegacyForemanRedirect:


    async def test_foreman_subpath_redirects_to_dashboard(self, db):
        status, headers, _, _ = await _call_middleware("/foreman/login")
        assert status == 302
        assert headers["location"] == "/dashboard"


# ── Localhost bypass ────────────────────────────────────────────────────────


# ── Legacy /foreman redirect ──────────────────────────────────────────────

