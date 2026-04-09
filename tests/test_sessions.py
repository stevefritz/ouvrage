"""Tests for session management and login/logout handlers.

Covers: session creation/validation/deletion, cookie parsing, login with argon2id,
logout, rate limiting, lockout, inactivity timeout, session expiry,
OAuth authorize → login redirect flow.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta

import pytest


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_scope(cookie: str | None = None, headers: list | None = None) -> dict:
    h = list(headers or [])
    if cookie:
        h.append((b"cookie", cookie.encode()))
    return {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": h,
    }


async def _call_handler(handler, method="POST", path="/", body=b"", headers=None, cookie=None):
    """Call an ASGI handler and return (status, headers_dict, body_bytes)."""
    h = list(headers or [])
    if cookie:
        h.append((b"cookie", cookie.encode()))

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": h,
    }

    status = None
    resp_headers = {}
    resp_body = b""

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        nonlocal status, resp_body
        if message["type"] == "http.response.start":
            status = message["status"]
            for k, v in message.get("headers", []):
                key = k.decode() if isinstance(k, bytes) else k
                val = v.decode() if isinstance(v, bytes) else v
                resp_headers[key] = val
        elif message["type"] == "http.response.body":
            resp_body += message.get("body", b"")

    await handler(scope, receive, send)
    return status, resp_headers, resp_body


def _json_body(data: dict, content_type=b"application/json") -> tuple[bytes, list]:
    return json.dumps(data).encode(), [(b"content-type", content_type)]


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
async def user_with_password(db):
    """Create a user with an argon2id-hashed password."""
    from argon2 import PasswordHasher
    ph = PasswordHasher()
    hashed = ph.hash("correcthorse")
    user = await db.create_user(
        email="alice@example.com",
        name="Alice",
        role="member",
        password_hash=hashed,
    )
    return user


# ── Session DB helpers ─────────────────────────────────────────────────────

class TestSessionCRUD:
    async def test_create_session(self, db):
        from switchboard.auth.sessions import create_session
        user = await db.create_user(email="u@test.com", name="U")
        session_id = await create_session(user["id"])
        assert isinstance(session_id, str)
        assert len(session_id) > 20

    async def test_create_session_unique(self, db):
        from switchboard.auth.sessions import create_session
        user = await db.create_user(email="u2@test.com", name="U2")
        sid1 = await create_session(user["id"])
        sid2 = await create_session(user["id"])
        assert sid1 != sid2

    async def test_delete_session(self, db):
        from switchboard.auth.sessions import create_session, delete_session, get_session_user
        user = await db.create_user(email="u3@test.com", name="U3")
        sid = await create_session(user["id"])

        scope = _make_scope(f"switchboard_session={sid}")
        assert await get_session_user(scope) is not None

        await delete_session(sid)
        assert await get_session_user(scope) is None

    async def test_delete_nonexistent_session(self, db):
        from switchboard.auth.sessions import delete_session
        # Should not raise
        await delete_session("nonexistent-session-id")


# ── Session Validation ─────────────────────────────────────────────────────

class TestGetSessionUser:
    async def test_valid_session_returns_user(self, db):
        from switchboard.auth.sessions import create_session, get_session_user
        user = await db.create_user(email="v@test.com", name="V")
        sid = await create_session(user["id"])

        scope = _make_scope(f"switchboard_session={sid}")
        result = await get_session_user(scope)
        assert result is not None
        assert result["id"] == user["id"]
        assert result["email"] == "v@test.com"
        assert result["name"] == "V"

    async def test_no_cookie_returns_none(self, db):
        from switchboard.auth.sessions import get_session_user
        scope = _make_scope(None)
        assert await get_session_user(scope) is None

    async def test_invalid_cookie_returns_none(self, db):
        from switchboard.auth.sessions import get_session_user
        scope = _make_scope("switchboard_session=bogus-session-id-not-in-db")
        assert await get_session_user(scope) is None

    async def test_expired_session_returns_none(self, db):
        from switchboard.auth.sessions import get_session_user
        from switchboard.db.connection import get_db as _get_db

        user = await db.create_user(email="exp@test.com", name="Exp")

        # Manually insert an expired session
        past = datetime.now(timezone.utc) - timedelta(days=1)
        iso = past.strftime("%Y-%m-%dT%H:%M:%SZ")
        async with _get_db() as conn:
            await conn.execute(
                """INSERT INTO sessions (session_id, user_id, created_at, expires_at, last_active)
                   VALUES (?, ?, ?, ?, ?)""",
                ("expired-session", user["id"], iso, iso, iso),
            )
            await conn.commit()

        scope = _make_scope("switchboard_session=expired-session")
        assert await get_session_user(scope) is None

    async def test_inactive_session_returns_none(self, db):
        """Session older than 24h inactivity should be rejected."""
        from switchboard.auth.sessions import get_session_user
        from switchboard.db.connection import get_db as _get_db

        user = await db.create_user(email="inactive@test.com", name="Inactive")

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=7)
        old_active = now - timedelta(hours=25)  # over 24h ago

        def iso(dt):
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        async with _get_db() as conn:
            await conn.execute(
                """INSERT INTO sessions (session_id, user_id, created_at, expires_at, last_active)
                   VALUES (?, ?, ?, ?, ?)""",
                ("inactive-session", user["id"], iso(now), iso(expires_at), iso(old_active)),
            )
            await conn.commit()

        scope = _make_scope("switchboard_session=inactive-session")
        assert await get_session_user(scope) is None

    async def test_recent_session_passes_inactivity(self, db):
        """Session active 23h ago is still valid."""
        from switchboard.auth.sessions import get_session_user
        from switchboard.db.connection import get_db as _get_db

        user = await db.create_user(email="recent@test.com", name="Recent")

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=7)
        recent_active = now - timedelta(hours=23)

        def iso(dt):
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        async with _get_db() as conn:
            await conn.execute(
                """INSERT INTO sessions (session_id, user_id, created_at, expires_at, last_active)
                   VALUES (?, ?, ?, ?, ?)""",
                ("recent-session", user["id"], iso(now), iso(expires_at), iso(recent_active)),
            )
            await conn.commit()

        scope = _make_scope("switchboard_session=recent-session")
        result = await get_session_user(scope)
        assert result is not None
        assert result["id"] == user["id"]


# ── Login Handler ──────────────────────────────────────────────────────────

class TestHandleLogin:
    async def test_login_success(self, db, user_with_password):
        from switchboard.auth.sessions import handle_login
        body, headers = _json_body({"email": "alice@example.com", "password": "correcthorse"})
        status, resp_headers, resp_body = await _call_handler(handle_login, body=body, headers=headers)

        assert status == 200
        data = json.loads(resp_body)
        assert "redirect" in data
        assert data["redirect"] == "/dashboard/"

        # Cookie should be set
        set_cookie = resp_headers.get("set-cookie", "")
        assert "switchboard_session=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=Lax" in set_cookie

    async def test_login_with_next_param(self, db, user_with_password):
        from switchboard.auth.sessions import handle_login
        next_url = "/oauth/authorize?client_id=test"
        body, headers = _json_body({
            "email": "alice@example.com",
            "password": "correcthorse",
            "next": next_url,
        })
        status, _, resp_body = await _call_handler(handle_login, body=body, headers=headers)
        assert status == 200
        data = json.loads(resp_body)
        assert data["redirect"] == next_url

    async def test_login_wrong_password(self, db, user_with_password):
        from switchboard.auth.sessions import handle_login
        body, headers = _json_body({"email": "alice@example.com", "password": "wrongpassword"})
        status, _, resp_body = await _call_handler(handle_login, body=body, headers=headers)
        assert status == 401
        data = json.loads(resp_body)
        assert data["error"] == "invalid_credentials"

    async def test_login_unknown_email(self, db):
        from switchboard.auth.sessions import handle_login
        body, headers = _json_body({"email": "nobody@example.com", "password": "anything"})
        status, _, resp_body = await _call_handler(handle_login, body=body, headers=headers)
        assert status == 401
        data = json.loads(resp_body)
        assert data["error"] == "invalid_credentials"

    async def test_login_generic_error_message(self, db, user_with_password):
        """Error should not reveal whether email exists or password is wrong."""
        from switchboard.auth.sessions import handle_login
        body, headers = _json_body({"email": "alice@example.com", "password": "wrong"})
        status, _, resp_body = await _call_handler(handle_login, body=body, headers=headers)
        data = json.loads(resp_body)
        # Same generic message regardless of what was wrong
        assert "Invalid email or password" in data.get("message", "")

    async def test_login_missing_fields(self, db):
        from switchboard.auth.sessions import handle_login
        body, headers = _json_body({"email": "alice@example.com"})
        status, _, _ = await _call_handler(handle_login, body=body, headers=headers)
        assert status == 400

    async def test_login_sets_session_in_db(self, db, user_with_password):
        """After successful login, a session row should exist."""
        from switchboard.auth.sessions import handle_login, get_session_user
        from http.cookies import SimpleCookie

        body, headers = _json_body({"email": "alice@example.com", "password": "correcthorse"})
        status, resp_headers, _ = await _call_handler(handle_login, body=body, headers=headers)

        assert status == 200
        set_cookie = resp_headers.get("set-cookie", "")

        # Extract session ID from Set-Cookie header
        cookie = SimpleCookie()
        cookie.load(set_cookie)
        morsel = cookie.get("switchboard_session")
        assert morsel is not None
        sid = morsel.value

        # Verify session is valid
        scope = _make_scope(f"switchboard_session={sid}")
        user = await get_session_user(scope)
        assert user is not None
        assert user["email"] == "alice@example.com"

    async def test_login_resets_failed_count(self, db, user_with_password):
        """Successful login resets failed_login_count to 0."""
        from switchboard.auth.sessions import handle_login
        from switchboard.db.users import update_user

        # Pre-set some failed attempts
        await update_user(user_with_password["id"], failed_login_count=3)

        body, headers = _json_body({"email": "alice@example.com", "password": "correcthorse"})
        status, _, _ = await _call_handler(handle_login, body=body, headers=headers)
        assert status == 200

        # Check count was reset
        from switchboard.db.users import get_user_by_email_with_auth
        user = await get_user_by_email_with_auth("alice@example.com")
        assert user["failed_login_count"] == 0

    async def test_login_no_password_hash(self, db):
        """User with no password_hash should be denied."""
        from switchboard.auth.sessions import handle_login
        # Bootstrap user has no password_hash
        user = await db.create_user(email="nohash@example.com", name="NoHash")
        body, headers = _json_body({"email": "nohash@example.com", "password": "anything"})
        status, _, resp_body = await _call_handler(handle_login, body=body, headers=headers)
        assert status == 401

    async def test_login_invalid_json(self, db):
        from switchboard.auth.sessions import handle_login
        body = b"not json at all"
        headers = [(b"content-type", b"application/json")]
        status, _, _ = await _call_handler(handle_login, body=body, headers=headers)
        assert status == 400

    async def test_login_open_redirect_blocked(self, db, user_with_password):
        """next= with external URL should be ignored, fallback to /dashboard/."""
        from switchboard.auth.sessions import handle_login
        body, headers = _json_body({
            "email": "alice@example.com",
            "password": "correcthorse",
            "next": "https://evil.com/steal-credentials",
        })
        status, _, resp_body = await _call_handler(handle_login, body=body, headers=headers)
        assert status == 200
        data = json.loads(resp_body)
        assert data["redirect"] == "/dashboard/"


# ── Rate Limiting / Lockout ────────────────────────────────────────────────

class TestRateLimiting:
    async def test_failed_attempts_increment_count(self, db, user_with_password):
        from switchboard.auth.sessions import handle_login
        from switchboard.db.users import get_user_by_email_with_auth

        for _ in range(3):
            body, headers = _json_body({"email": "alice@example.com", "password": "wrong"})
            await _call_handler(handle_login, body=body, headers=headers)

        user = await get_user_by_email_with_auth("alice@example.com")
        assert user["failed_login_count"] == 3

    async def test_lockout_after_5_failures(self, db, user_with_password):
        from switchboard.auth.sessions import handle_login
        from switchboard.db.users import get_user_by_email_with_auth

        # 5 failures → locked
        for _ in range(5):
            body, headers = _json_body({"email": "alice@example.com", "password": "wrong"})
            await _call_handler(handle_login, body=body, headers=headers)

        # 6th attempt should return 429
        body, headers = _json_body({"email": "alice@example.com", "password": "wrong"})
        status, _, resp_body = await _call_handler(handle_login, body=body, headers=headers)
        assert status == 429
        data = json.loads(resp_body)
        assert data["error"] == "account_locked"

        # locked_until should be set
        user = await get_user_by_email_with_auth("alice@example.com")
        assert user["locked_until"] is not None

    async def test_correct_password_during_lockout_rejected(self, db, user_with_password):
        from switchboard.auth.sessions import handle_login
        from switchboard.db.users import update_user

        # Pre-lock the account
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        locked_until = future.strftime("%Y-%m-%dT%H:%M:%SZ")
        await update_user(user_with_password["id"], locked_until=locked_until, failed_login_count=5)

        # Even correct password should be rejected
        body, headers = _json_body({"email": "alice@example.com", "password": "correcthorse"})
        status, _, resp_body = await _call_handler(handle_login, body=body, headers=headers)
        assert status == 429
        data = json.loads(resp_body)
        assert data["error"] == "account_locked"

    async def test_expired_lockout_allows_login(self, db, user_with_password):
        from switchboard.auth.sessions import handle_login
        from switchboard.db.users import update_user

        # Set a past lockout
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        locked_until = past.strftime("%Y-%m-%dT%H:%M:%SZ")
        await update_user(user_with_password["id"], locked_until=locked_until, failed_login_count=5)

        body, headers = _json_body({"email": "alice@example.com", "password": "correcthorse"})
        status, _, _ = await _call_handler(handle_login, body=body, headers=headers)
        assert status == 200


# ── Logout Handler ─────────────────────────────────────────────────────────

class TestHandleLogout:
    async def test_logout_clears_session(self, db):
        from switchboard.auth.sessions import create_session, handle_logout, get_session_user

        user = await db.create_user(email="logout@test.com", name="Logout")
        sid = await create_session(user["id"])

        cookie = f"switchboard_session={sid}"
        status, resp_headers, _ = await _call_handler(handle_logout, cookie=cookie)
        assert status == 200

        # Cookie should be cleared
        set_cookie = resp_headers.get("set-cookie", "")
        assert "switchboard_session=" in set_cookie
        assert "Max-Age=0" in set_cookie

        # Session should no longer be valid
        scope = _make_scope(f"switchboard_session={sid}")
        assert await get_session_user(scope) is None

    async def test_logout_without_cookie_is_ok(self, db):
        from switchboard.auth.sessions import handle_logout
        status, _, _ = await _call_handler(handle_logout)
        assert status == 200

    async def test_logout_returns_ok_json(self, db):
        from switchboard.auth.sessions import handle_logout
        status, _, body = await _call_handler(handle_logout)
        assert status == 200
        data = json.loads(body)
        assert data.get("ok") is True


# ── Cookie Safety ──────────────────────────────────────────────────────────

class TestCookieSafety:
    async def test_cookie_attributes(self, db, user_with_password):
        from switchboard.auth.sessions import handle_login
        body, headers = _json_body({"email": "alice@example.com", "password": "correcthorse"})
        _, resp_headers, _ = await _call_handler(handle_login, body=body, headers=headers)

        set_cookie = resp_headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=Lax" in set_cookie
        assert "Max-Age=" in set_cookie
        assert "Path=/" in set_cookie

    async def test_cookie_max_age_is_7_days(self, db, user_with_password):
        from switchboard.auth.sessions import handle_login, SESSION_TTL_DAYS
        body, headers = _json_body({"email": "alice@example.com", "password": "correcthorse"})
        _, resp_headers, _ = await _call_handler(handle_login, body=body, headers=headers)

        set_cookie = resp_headers.get("set-cookie", "")
        expected_age = str(SESSION_TTL_DAYS * 86400)
        assert f"Max-Age={expected_age}" in set_cookie


# ── OAuth → Login Redirect ─────────────────────────────────────────────────

class TestOAuthLoginRedirect:
    @pytest.fixture(autouse=True)
    def oauth_env(self, tmp_path):
        os.environ["OAUTH_BASE_URL"] = "https://switchboard.test"
        os.environ["OAUTH_RSA_KEY_PATH"] = str(tmp_path / "test_rsa_key.pem")
        import switchboard.config.settings as _s
        import switchboard.auth.oauth as _oauth
        _s.OAUTH_BASE_URL = "https://switchboard.test"
        _s.OAUTH_RSA_KEY_PATH = os.environ["OAUTH_RSA_KEY_PATH"]
        _oauth.OAUTH_BASE_URL = "https://switchboard.test"
        _oauth.OAUTH_RSA_KEY_PATH = os.environ["OAUTH_RSA_KEY_PATH"]
        _oauth._rsa_private_key = None
        _oauth._rsa_public_jwk = None
        from switchboard.auth.oauth import init_oauth_keys, seed_default_client
        init_oauth_keys()
        yield
        os.environ.pop("OAUTH_BASE_URL", None)
        os.environ.pop("OAUTH_RSA_KEY_PATH", None)

    async def test_authorize_no_session_redirects_to_login(self, db):
        from switchboard.auth.oauth import handle_authorize, seed_default_client
        await seed_default_client()

        query = "response_type=code&client_id=claude-mcp&redirect_uri=https://claude.ai/oauth/callback&scope=openid&state=abc"
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/oauth/authorize",
            "query_string": query.encode(),
            "headers": [],
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
        assert location.startswith("/dashboard/login?next=")
        assert "oauth%2Fauthorize" in location or "oauth/authorize" in location

    async def test_authorize_no_session_encodes_full_url(self, db):
        """The next= param should contain the full authorize URL with all params."""
        from switchboard.auth.oauth import handle_authorize, seed_default_client
        from urllib.parse import urlparse, parse_qs, unquote
        await seed_default_client()

        query = "response_type=code&client_id=claude-mcp&redirect_uri=https://claude.ai/oauth/callback&scope=openid&state=xyz123"
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/oauth/authorize",
            "query_string": query.encode(),
            "headers": [],
        }

        location = None

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            nonlocal location
            if message["type"] == "http.response.start":
                for k, v in message.get("headers", []):
                    key = k.decode() if isinstance(k, bytes) else k
                    val = v.decode() if isinstance(v, bytes) else v
                    if key == "location":
                        location = val

        await handle_authorize(scope, receive, send)

        parsed = urlparse(location)
        next_val = unquote(parse_qs(parsed.query)["next"][0])
        # The next URL should be the full authorize URL with all params
        assert "client_id=claude-mcp" in next_val
        assert "state=xyz123" in next_val

    async def test_authorize_with_valid_session_issues_code(self, db):
        """After login, session injected → authorize issues code."""
        from switchboard.auth.oauth import handle_authorize, seed_default_client
        await seed_default_client()

        user = await db.create_user(email="oauth@test.com", name="OAuth User")

        query = "response_type=code&client_id=claude-mcp&redirect_uri=https://claude.ai/oauth/callback&scope=openid&state=state1"
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/oauth/authorize",
            "query_string": query.encode(),
            "headers": [],
            "oauth_user_id": user["id"],  # injected by session middleware
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
        assert "code=" in location
        assert location.startswith("https://claude.ai/oauth/callback")


# ── Auth middleware routing (unique coverage from test_dashboard_auth.py) ──────

async def _call_middleware(path, cookie=None, client=("10.0.0.1", 12345), query_string=b""):
    """Call auth_middleware and return (status, headers_dict, body_bytes)."""
    from switchboard.auth.middleware import auth_middleware

    headers = []
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": query_string,
        "headers": headers,
        "client": client,
    }

    status = None
    resp_headers = {}
    body = b""

    async def inner_app(s, r, snd):
        await snd({"type": "http.response.start", "status": 200, "headers": []})
        await snd({"type": "http.response.body", "body": b"OK"})

    app = auth_middleware(inner_app)

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


class TestAuthMiddlewareBehavior:

    async def test_dashboard_api_401_is_json(self, db):
        """401 from /dashboard/api must be JSON, not HTML."""
        import json
        _, _, body = await _call_middleware("/dashboard/api/tasks")
        data = json.loads(body)
        assert "error" in data
        assert data["error"] == "authentication_required"

    async def test_dashboard_static_passes_without_session(self, db):
        """Static assets (file extension) pass through without auth."""
        status, _, body = await _call_middleware("/dashboard/app.js")
        assert status == 200
        assert body == b"OK"

    async def test_foreman_legacy_redirects_to_dashboard(self, db):
        """/foreman paths redirect to /dashboard."""
        status, headers, _ = await _call_middleware("/foreman")
        assert status == 302
        assert headers["location"] == "/dashboard"

    async def test_localhost_does_not_bypass_dashboard_api(self, db):
        """Localhost bypass is scoped to /mcp/worker — /dashboard/api still requires session."""
        status, _, _ = await _call_middleware(
            "/dashboard/api/tasks", client=("127.0.0.1", 5000)
        )
        assert status == 401
