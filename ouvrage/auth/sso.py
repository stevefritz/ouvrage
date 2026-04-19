"""SSO endpoint handler for Ouvrage.

GET /auth/sso?token=<jwt>

Only active when AUTH_MODE=saas. In local mode, returns 404.

Flow:
1. Extract token from query param (400 if missing)
2. Fetch JWKS from CONTROL_PLANE_JWKS (cached 1 hour)
3. Validate JWT: RS256 signature, expiry, audience=INSTANCE_SLUG
4. Upsert user from claims (email, role)
5. Create session, set cookie
6. Redirect to ?redirect= param (relative only) or /dashboard
"""

import json
import logging
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt

from ouvrage.config.settings import AUTH_MODE, CONTROL_PLANE_JWKS, INSTANCE_SLUG
from ouvrage.auth.sessions import create_session, _build_session_cookie
from ouvrage.db.users import get_user_by_email, create_user, update_user

logger = logging.getLogger("ouvrage.auth.sso")

# ── JWKS cache ──────────────────────────────────────────────────────────────

_jwks_cache: dict | None = None
_jwks_fetched_at: float = 0.0
JWKS_TTL = 3600  # 1 hour


async def _fetch_jwks() -> dict:
    """Fetch JWKS from CONTROL_PLANE_JWKS URL (no caching — caller manages cache)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(CONTROL_PLANE_JWKS, timeout=10)
        resp.raise_for_status()
        return resp.json()


async def get_jwks(force_refresh: bool = False) -> dict:
    """Return cached JWKS, refreshing if stale or forced."""
    global _jwks_cache, _jwks_fetched_at

    if not force_refresh and _jwks_cache and (time.time() - _jwks_fetched_at) < JWKS_TTL:
        return _jwks_cache

    _jwks_cache = await _fetch_jwks()
    _jwks_fetched_at = time.time()
    logger.info("JWKS refreshed: %d keys", len(_jwks_cache.get("keys", [])))
    return _jwks_cache


def _invalidate_jwks_cache() -> None:
    """Force the next get_jwks() call to re-fetch."""
    global _jwks_fetched_at
    _jwks_fetched_at = 0.0


# ── JWT validation ──────────────────────────────────────────────────────────

async def _validate_jwt(token: str) -> dict | None:
    """Validate a control-plane JWT. Returns claims dict or None.

    Validates:
    - RS256 signature against JWKS from CONTROL_PLANE_JWKS
    - exp (expiry)
    - aud (audience) must match INSTANCE_SLUG

    On kid miss with cached JWKS, refreshes once and retries.
    Returns None on any validation failure (never leaks details to caller).
    """
    try:
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        jwks = await get_jwks()
        key = _find_key(jwks, kid)

        if key is None:
            # Key not found — might be stale cache; refresh once and retry
            logger.info("kid=%r not found in cached JWKS, refreshing", kid)
            jwks = await get_jwks(force_refresh=True)
            key = _find_key(jwks, kid)

        if key is None:
            logger.warning("No JWKS key found for kid=%r", kid)
            return None

        decode_opts = {
            "algorithms": ["RS256"],
            "options": {"verify_exp": True, "verify_aud": bool(INSTANCE_SLUG)},
        }
        if INSTANCE_SLUG:
            decode_opts["audience"] = INSTANCE_SLUG

        claims = jwt.decode(token, key, **decode_opts)
        return claims

    except jwt.ExpiredSignatureError:
        logger.warning("SSO JWT expired")
        return None
    except jwt.InvalidAudienceError:
        logger.warning("SSO JWT audience mismatch (expected %r)", INSTANCE_SLUG)
        return None
    except jwt.InvalidSignatureError:
        logger.warning("SSO JWT signature invalid")
        _invalidate_jwks_cache()
        return None
    except jwt.InvalidTokenError as e:
        logger.warning("SSO JWT invalid: %s: %s", type(e).__name__, e)
        return None
    except Exception as e:
        logger.error("SSO JWT validation error: %s: %s", type(e).__name__, e)
        return None


def _find_key(jwks: dict, kid: str | None):
    """Find and return the RSA public key matching kid from a JWKS dict."""
    for jwk in jwks.get("keys", []):
        if kid is None or jwk.get("kid") == kid:
            try:
                return jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
            except Exception as e:
                logger.warning("Failed to parse JWK kid=%r: %s", jwk.get("kid"), e)
    return None


# ── User upsert ─────────────────────────────────────────────────────────────

async def _upsert_user(email: str, role: str) -> dict:
    """Look up user by email; create if missing; update role if changed."""
    user = await get_user_by_email(email)
    if user is None:
        # First SSO for this email — create the user
        name = email.split("@")[0]
        user = await create_user(email=email, name=name, role=role)
        logger.info("SSO created new user: email=%r role=%r", email, role)
    elif user.get("role") != role:
        # Role changed in control plane — sync it
        user = await update_user(user["id"], role=role)
        logger.info("SSO updated user role: email=%r role=%r", email, role)
    return user


# ── Redirect safety ─────────────────────────────────────────────────────────

def _safe_redirect(redirect: str | None) -> str:
    """Allow only relative paths to prevent open redirect. Returns /dashboard if unsafe."""
    if not redirect:
        return "/dashboard"
    try:
        parsed = urlparse(redirect)
        if parsed.scheme or parsed.netloc:
            return "/dashboard"
        if not parsed.path.startswith("/"):
            return "/dashboard"
        return redirect
    except Exception:
        return "/dashboard"


# ── ASGI helpers ─────────────────────────────────────────────────────────────

async def _send_json(send, status: int, body: dict):
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [[b"content-type", b"application/json"]],
    })
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


async def _send_redirect(send, location: str, extra_headers: list | None = None):
    headers = [[b"location", location.encode()]]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": 302, "headers": headers})
    await send({"type": "http.response.body", "body": b""})


# ── Handler ──────────────────────────────────────────────────────────────────

async def handle_sso(scope, receive, send):
    """GET /auth/sso?token=<jwt>[&redirect=<path>]

    Only active in AUTH_MODE=saas. Returns 404 in local mode.
    """
    # Local mode: 404
    if AUTH_MODE != "saas":
        await send({
            "type": "http.response.start",
            "status": 404,
            "headers": [[b"content-type", b"text/plain"]],
        })
        await send({"type": "http.response.body", "body": b"Not Found"})
        return

    # Parse query params
    qs = scope.get("query_string", b"").decode("utf-8", errors="replace")
    params = parse_qs(qs, keep_blank_values=False)
    token = params.get("token", [None])[0]
    redirect_param = params.get("redirect", [None])[0]

    if not token:
        await _send_json(send, 400, {"error": "missing_token", "message": "token query param required"})
        return

    # Validate JWT
    if not CONTROL_PLANE_JWKS:
        logger.error("CONTROL_PLANE_JWKS is not configured")
        await _send_json(send, 500, {"error": "server_error", "message": "SSO not configured"})
        return

    claims = await _validate_jwt(token)
    if claims is None:
        await _send_json(send, 401, {"error": "invalid_token", "message": "Token validation failed"})
        return

    # Extract claims
    email = claims.get("email")
    role = claims.get("role", "member")

    if not email:
        logger.warning("SSO JWT missing email claim")
        await _send_json(send, 401, {"error": "invalid_token", "message": "Token validation failed"})
        return

    # Upsert user
    try:
        user = await _upsert_user(email=email.strip().lower(), role=role)
    except Exception as e:
        logger.error("SSO user upsert failed: %s", e)
        await _send_json(send, 500, {"error": "server_error", "message": "Internal error"})
        return

    # Create session
    try:
        session_id = await create_session(user["id"])
    except Exception as e:
        logger.error("SSO session creation failed: %s", e)
        await _send_json(send, 500, {"error": "server_error", "message": "Internal error"})
        return

    # Build redirect and cookie
    redirect_to = _safe_redirect(redirect_param)
    cookie_header = _build_session_cookie(session_id)

    await _send_redirect(send, redirect_to, extra_headers=[[b"set-cookie", cookie_header]])
