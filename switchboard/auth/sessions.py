"""Session management and login/logout handlers for Switchboard.

Provides:
- Cookie-based session creation, validation, and deletion
- POST /auth/login — argon2id password verification, rate limiting, session creation
- POST /auth/logout — session deletion + cookie clearing
- get_session_user(scope) → user dict | None — session validation helper
"""

import json
import logging
import secrets
from datetime import datetime, timezone, timedelta
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

from switchboard.db.connection import get_db
from switchboard.db._helpers import now_iso
from switchboard.db.users import get_user_by_email_with_auth, update_user

logger = logging.getLogger("switchboard.auth.sessions")

# ── Constants ──────────────────────────────────────────────────────────────

SESSION_COOKIE_NAME = "switchboard_session"
SESSION_TTL_DAYS = 7
SESSION_INACTIVITY_HOURS = 24
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15


# ── Session DB helpers ─────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def create_session(user_id: int) -> str:
    """Create a new session for user_id. Returns session_id."""
    session_id = secrets.token_urlsafe(32)
    now = _now_utc()
    expires_at = now + timedelta(days=SESSION_TTL_DAYS)

    async with get_db() as db:
        await db.execute(
            """INSERT INTO sessions (session_id, user_id, created_at, expires_at, last_active)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, user_id, _iso(now), _iso(expires_at), _iso(now)),
        )
        await db.commit()

    return session_id


async def delete_session(session_id: str) -> None:
    """Delete a session by session_id."""
    async with get_db() as db:
        await db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        await db.commit()


async def _touch_session(session_id: str) -> None:
    """Update last_active to now (best-effort)."""
    try:
        async with get_db() as db:
            await db.execute(
                "UPDATE sessions SET last_active = ? WHERE session_id = ?",
                (_iso(_now_utc()), session_id),
            )
            await db.commit()
    except Exception:
        pass  # best-effort, never fail a request over this


async def get_session_user(scope) -> dict | None:
    """Validate session cookie from ASGI scope headers.

    Checks:
    1. Cookie present and session exists in DB
    2. Session not expired (expires_at > now)
    3. Session active within last 24h (last_active)

    On success: touches last_active and returns user dict (id, email, name, role).
    Returns None if no valid session.
    """
    session_id = _extract_session_cookie(scope)
    if not session_id:
        return None

    now = _now_utc()
    inactivity_cutoff = now - timedelta(hours=SESSION_INACTIVITY_HOURS)

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT s.session_id, s.user_id, s.expires_at, s.last_active,
                      u.id, u.email, u.name, u.role
               FROM sessions s
               JOIN users u ON s.user_id = u.id
               WHERE s.session_id = ?""",
            (session_id,),
        )

    if not rows:
        return None

    row = dict(rows[0])

    # Check expiry
    try:
        expires_at = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if now >= expires_at:
            return None
    except (ValueError, AttributeError):
        return None

    # Check inactivity
    try:
        last_active = datetime.fromisoformat(row["last_active"].replace("Z", "+00:00"))
        if last_active < inactivity_cutoff:
            return None
    except (ValueError, AttributeError):
        return None

    # Touch last_active (fire-and-forget)
    import asyncio
    asyncio.create_task(_touch_session(session_id))

    return {
        "id": row["user_id"],
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
    }


def _extract_session_cookie(scope) -> str | None:
    """Extract switchboard_session value from ASGI scope headers."""
    headers = dict(scope.get("headers", []))
    cookie_header = headers.get(b"cookie", b"").decode("utf-8", errors="replace")
    if not cookie_header:
        return None

    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None

    morsel = cookie.get(SESSION_COOKIE_NAME)
    return morsel.value if morsel else None


# ── ASGI helpers ───────────────────────────────────────────────────────────

async def _read_body(receive) -> bytes:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    return body


async def _send_json(send, status: int, body: dict, extra_headers: list | None = None):
    headers = [[b"content-type", b"application/json"]]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


def _build_session_cookie(session_id: str, max_age: int | None = None, clear: bool = False) -> bytes:
    """Build a Set-Cookie header value for the session cookie."""
    if clear:
        value = f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0"
    else:
        age = max_age if max_age is not None else SESSION_TTL_DAYS * 86400
        value = (
            f"{SESSION_COOKIE_NAME}={session_id}; Path=/; HttpOnly; Secure; "
            f"SameSite=Lax; Max-Age={age}"
        )
    return value.encode()


# ── Login handler ──────────────────────────────────────────────────────────

async def handle_login(scope, receive, send):
    """POST /auth/login — Validate credentials, create session, set cookie.

    Request body: JSON {"email": "...", "password": "...", "next": "..."}
    or URL-encoded form (also accepted).

    Success: 200 {"redirect": "/foreman/" or next param}
    Failure: 401 {"error": "invalid_credentials"}
    Locked:  429 {"error": "account_locked", "message": "..."}
    """
    try:
        from argon2 import PasswordHasher
        from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
    except ImportError:
        await _send_json(send, 500, {"error": "server_error", "message": "argon2-cffi not installed"})
        return

    body = await _read_body(receive)

    # Accept JSON or form-encoded
    content_type = b""
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-type":
            content_type = value
            break

    email = None
    password = None
    next_url = None

    if b"application/json" in content_type:
        try:
            data = json.loads(body)
            email = data.get("email", "").strip().lower()
            password = data.get("password", "")
            next_url = data.get("next", "")
        except (json.JSONDecodeError, AttributeError):
            await _send_json(send, 400, {"error": "invalid_request", "message": "Invalid JSON"})
            return
    else:
        params = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        email = params.get("email", [""])[0].strip().lower()
        password = params.get("password", [""])[0]
        next_url = params.get("next", [""])[0]

    if not email or not password:
        await _send_json(send, 400, {"error": "invalid_request", "message": "email and password required"})
        return

    # Look up user (with auth fields)
    user = await get_user_by_email_with_auth(email)

    if not user:
        # User not found — return generic message (don't reveal existence)
        await _send_json(send, 401, {"error": "invalid_credentials", "message": "Invalid email or password"})
        return

    # Check rate limit: locked_until in the future
    now = _now_utc()
    locked_until_str = user.get("locked_until")
    if locked_until_str:
        try:
            locked_until = datetime.fromisoformat(locked_until_str.replace("Z", "+00:00"))
            if now < locked_until:
                remaining = int((locked_until - now).total_seconds() // 60) + 1
                await _send_json(send, 429, {
                    "error": "account_locked",
                    "message": f"Account locked. Try again in {remaining} minute(s).",
                })
                return
        except (ValueError, AttributeError):
            pass  # malformed timestamp — treat as not locked

    # Verify password
    if not user.get("password_hash"):
        # No password set (e.g. bootstrap owner) — deny
        await _send_json(send, 401, {"error": "invalid_credentials", "message": "Invalid email or password"})
        return

    ph = PasswordHasher()
    password_ok = False
    try:
        password_ok = ph.verify(user["password_hash"], password)
    except VerifyMismatchError:
        password_ok = False
    except (VerificationError, InvalidHashError):
        password_ok = False

    if not password_ok:
        # Increment failed count; lock if threshold reached
        new_count = (user.get("failed_login_count") or 0) + 1
        updates = {"failed_login_count": new_count}
        if new_count >= LOGIN_MAX_ATTEMPTS:
            lock_until = now + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
            updates["locked_until"] = _iso(lock_until)
            logger.warning("Account %s locked after %d failed attempts", email, new_count)
        await update_user(user["id"], **updates)

        await _send_json(send, 401, {"error": "invalid_credentials", "message": "Invalid email or password"})
        return

    # Password correct — reset rate limit, create session
    await update_user(user["id"], failed_login_count=0, locked_until=None)

    session_id = await create_session(user["id"])

    # Determine redirect target
    redirect = _safe_next_url(next_url) or "/foreman/"

    cookie_header = _build_session_cookie(session_id)
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            [b"content-type", b"application/json"],
            [b"set-cookie", cookie_header],
        ],
    })
    await send({
        "type": "http.response.body",
        "body": json.dumps({"redirect": redirect}).encode(),
    })


def _safe_next_url(next_url: str | None) -> str | None:
    """Validate next URL — only allow relative paths (no open redirect)."""
    if not next_url:
        return None
    try:
        parsed = urlparse(next_url)
        # Allow only relative URLs (no scheme/netloc)
        if parsed.scheme or parsed.netloc:
            return None
        if not parsed.path.startswith("/"):
            return None
        return next_url
    except Exception:
        return None


# ── Logout handler ─────────────────────────────────────────────────────────

async def handle_logout(scope, receive, send):
    """POST /auth/logout — Delete session and clear cookie."""
    await _read_body(receive)  # drain body

    session_id = _extract_session_cookie(scope)
    if session_id:
        await delete_session(session_id)

    clear_cookie = _build_session_cookie("", clear=True)
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [
            [b"content-type", b"application/json"],
            [b"set-cookie", clear_cookie],
        ],
    })
    await send({"type": "http.response.body", "body": b'{"ok":true}'})
