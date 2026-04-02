"""
Auth middleware for Switchboard.

Two layers of protection, always active:

1. Session auth (always active):
   - /dashboard* (except /dashboard/login): requires valid session cookie.
     No session → 302 redirect to /dashboard/login?next={path}
   - /dashboard/api/*: requires valid session cookie.
     No session → 401 JSON {"error": "authentication_required"}
   - /dashboard/static assets: pass through, no auth.

2. Bearer JWT auth (always active):
   - All other paths require a valid Bearer token.
   - If AUTH_ISSUER_URL is unset or points to self: validates against local RSA
     key (zero HTTP roundtrip, using the key from the built-in OAuth server).
   - If AUTH_ISSUER_URL is external: fetches JWKS via OIDC discovery (legacy
     Authelia mode for backward compatibility).

Both layers bypass localhost connections (CC workers on the same host).
"""

import json
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from switchboard.auth.sessions import get_session_user

logger = logging.getLogger("switchboard.auth")

# ── Configuration ──────────────────────────────────────────────────────────

from switchboard.config.settings import (
    AUTH_ISSUER_URL,
    AUTH_AUDIENCE,
    AUTH_REQUIRED_SCOPES,
    RESOURCE_URL,
    OAUTH_BASE_URL,
    AUTH_MODE,
    CONTROL_PLANE_URL,
)


def is_auth_enabled() -> bool:
    """Bearer JWT auth is always active — either self-issued or external."""
    return True


# ── Self-vs-external issuer detection ──────────────────────────────────────

def _is_self_issuer() -> bool:
    """Return True when JWT validation should use the local RSA key.

    Self-issuer conditions (in order):
    - AUTH_ISSUER_URL is unset
    - AUTH_ISSUER_URL starts with localhost or 127.0.0.1
    - AUTH_ISSUER_URL matches OAUTH_BASE_URL (the built-in OAuth server)
    - AUTH_ISSUER_URL matches RESOURCE_URL

    Everything else is treated as an external issuer (remote JWKS fetch).
    """
    if not AUTH_ISSUER_URL:
        return True
    issuer = AUTH_ISSUER_URL.rstrip("/").lower()
    if issuer.startswith("http://localhost") or issuer.startswith("http://127.0.0.1"):
        return True
    if OAUTH_BASE_URL and issuer == OAUTH_BASE_URL.rstrip("/").lower():
        return True
    if RESOURCE_URL and issuer == RESOURCE_URL.rstrip("/").lower():
        return True
    return False


def _get_local_jwks() -> dict[str, Any]:
    """Return JWKS built from the local RSA key (no HTTP)."""
    from switchboard.auth.oauth import get_jwks
    return get_jwks()


def _get_self_base_url() -> str:
    """Return the expected issuer URL for self-issued tokens."""
    from switchboard.auth.oauth import _get_base_url
    return _get_base_url()


async def _is_token_revoked(jti: str) -> bool:
    """Check the oauth_tokens table for revocation. Returns True if revoked."""
    try:
        from switchboard.db.connection import get_db
        async with get_db() as db:
            rows = await db.execute_fetchall(
                "SELECT revoked FROM oauth_tokens WHERE access_token_jti = ?",
                (jti,),
            )
            return bool(rows and rows[0]["revoked"])
    except Exception as e:
        logger.warning(f"Revocation check failed (jti={jti!r}): {e}")
        return False  # Fail open — JWT was still cryptographically valid


# ── Remote JWKS cache ──────────────────────────────────────────────────────

_jwks_cache: dict[str, Any] = {}
_jwks_cache_time: float = 0
_JWKS_CACHE_TTL = 3600  # 1 hour


async def _get_remote_jwks() -> dict[str, Any]:
    """Fetch JWKS from external OIDC provider (with 1-hour cache)."""
    global _jwks_cache, _jwks_cache_time

    if _jwks_cache and (time.time() - _jwks_cache_time) < _JWKS_CACHE_TTL:
        return _jwks_cache

    # Discover OIDC configuration
    async with httpx.AsyncClient() as client:
        oidc_url = f"{AUTH_ISSUER_URL.rstrip('/')}/.well-known/openid-configuration"
        logger.info(f"Fetching OIDC config from {oidc_url}")
        oidc_resp = await client.get(oidc_url)
        oidc_resp.raise_for_status()
        oidc_config = oidc_resp.json()
        jwks_uri = oidc_config["jwks_uri"]
        logger.info(f"OIDC issuer={oidc_config.get('issuer')!r}, jwks_uri={jwks_uri}")

        jwks_resp = await client.get(jwks_uri)
        jwks_resp.raise_for_status()
        _jwks_cache = jwks_resp.json()
        _jwks_cache_time = time.time()
        logger.info(f"Cached {len(_jwks_cache.get('keys', []))} JWKS keys")

    return _jwks_cache


# Keep old name as alias for backward compatibility with any call sites
_get_jwks = _get_remote_jwks


async def verify_token(token: str) -> dict | None:
    """Validate a JWT against local or remote JWKS. Returns claims dict or None.

    - Self-issued (AUTH_ISSUER_URL unset or self): validates against the local
      RSA key loaded by the built-in OAuth server. Zero HTTP roundtrip.
    - External (AUTH_ISSUER_URL set to an external host): fetches JWKS via
      OIDC discovery (legacy Authelia compat).

    After signature validation, checks the oauth_tokens table for revocation
    when the token carries a `jti` claim. No jti → skip (backward compat).
    """
    try:
        # Determine JWKS source and expected issuer
        if _is_self_issuer():
            jwks_data = _get_local_jwks()
            expected_issuer = _get_self_base_url()
        else:
            jwks_data = await _get_remote_jwks()
            expected_issuer = AUTH_ISSUER_URL.rstrip("/")

        # Resolve the signing key by kid
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        key = None
        for jwk in jwks_data.get("keys", []):
            if jwk.get("kid") == kid:
                key = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
                break

        if key is None and not _is_self_issuer():
            # Remote key not found — maybe rotated; clear cache and retry once
            global _jwks_cache_time
            _jwks_cache_time = 0
            jwks_data = await _get_remote_jwks()
            for jwk in jwks_data.get("keys", []):
                if jwk.get("kid") == kid:
                    key = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
                    break

        if key is None:
            logger.warning(f"No JWKS key found for kid={kid!r}")
            return None

        # Log what we're about to validate against
        unverified_claims = jwt.decode(token, options={"verify_signature": False})
        logger.debug(f"Token iss={unverified_claims.get('iss')!r}, expected={expected_issuer!r}")
        logger.debug(f"Token aud={unverified_claims.get('aud')!r}, verify_aud={bool(AUTH_AUDIENCE)}")
        logger.debug(f"Token alg={unverified_header.get('alg')!r}, kid={kid!r}")

        decode_opts = {
            "algorithms": ["RS256"],
            "options": {"verify_exp": True, "verify_iss": True, "verify_aud": bool(AUTH_AUDIENCE)},
            "issuer": expected_issuer,
        }
        if AUTH_AUDIENCE:
            decode_opts["audience"] = AUTH_AUDIENCE

        claims = jwt.decode(token, key, **decode_opts)

        # Check required scopes
        if AUTH_REQUIRED_SCOPES:
            token_scopes = claims.get("scope", "").split()
            if not all(s in token_scopes for s in AUTH_REQUIRED_SCOPES):
                logger.warning(f"Insufficient scopes: have={token_scopes}, need={AUTH_REQUIRED_SCOPES}")
                return None

        # Revocation check: if jti present, look up in oauth_tokens table
        jti = claims.get("jti")
        if jti and await _is_token_revoked(jti):
            logger.warning(f"Token rejected: jti={jti!r} is revoked")
            return None

        logger.debug(f"Token verified for client={claims.get('client_id', claims.get('azp', 'unknown'))}")
        return claims

    except jwt.ExpiredSignatureError:
        logger.warning("Token expired")
        return None
    except jwt.InvalidIssuerError as e:
        logger.warning(f"Issuer mismatch: {e}")
        return None
    except jwt.InvalidAudienceError as e:
        logger.warning(f"Audience mismatch: {e}")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Token validation failed: {type(e).__name__}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected auth error: {type(e).__name__}: {e}")
        return None


# ── ASGI helpers ───────────────────────────────────────────────────────────

async def _send_json(send, status: int, body: dict, extra_headers: list | None = None):
    headers = [[b"content-type", b"application/json"]]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


def _www_authenticate_header(error: str | None = None, error_description: str | None = None) -> list:
    """Build WWW-Authenticate header per RFC 6750."""
    parts = ["Bearer"]
    params = []
    if RESOURCE_URL:
        params.append(f'resource="{RESOURCE_URL}"')
    if error:
        params.append(f'error="{error}"')
    if error_description:
        params.append(f'error_description="{error_description}"')
    if params:
        parts[0] += " " + ", ".join(params)
    return [b"www-authenticate", parts[0].encode()]


# ── Protected resource metadata ────────────────────────────────────────────

def _protected_resource_metadata() -> dict:
    """RFC 9728 OAuth Protected Resource Metadata.

    When OAUTH_BASE_URL is set (or AUTH_ISSUER_URL is unset/self), points to
    self as the authorization server.  Otherwise falls back to external
    AUTH_ISSUER_URL (legacy Authelia compat).
    """
    if OAUTH_BASE_URL or _is_self_issuer():
        base = (OAUTH_BASE_URL or _get_self_base_url()).rstrip("/")
        return {
            "resource": RESOURCE_URL or base,
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
        }
    # External issuer
    meta = {
        "resource": RESOURCE_URL or AUTH_ISSUER_URL,
        "authorization_servers": [AUTH_ISSUER_URL.rstrip("/")],
        "bearer_methods_supported": ["header"],
    }
    if AUTH_REQUIRED_SCOPES:
        meta["scopes_supported"] = AUTH_REQUIRED_SCOPES
    return meta


# ── SaaS helpers ───────────────────────────────────────────────────────────

def _get_instance_url(scope: dict) -> str:
    """Derive this instance's base URL from the request host header.

    Returns e.g. 'https://tenant.foreman.dev'. Falls back to an empty string
    if the host header is absent (should not happen in normal usage).
    """
    headers = dict(scope.get("headers", []))
    host = headers.get(b"host", b"").decode("utf-8", errors="replace")
    if not host:
        return ""
    # Assume HTTPS in SaaS mode (tenants are always behind TLS).
    return f"https://{host}"


def _saas_redirect_url(scope: dict) -> str:
    """Build the control-plane redirect URL for unauthenticated SaaS requests.

    Returns: {CONTROL_PLANE_URL}/login?redirect={instance_url}/auth/sso
    """
    instance_url = _get_instance_url(scope)
    cp = (CONTROL_PLANE_URL or "").rstrip("/")
    return f"{cp}/login?redirect={quote(instance_url + '/auth/sso', safe='')}"


# ── Middleware ──────────────────────────────────────────────────────────────

UNPROTECTED_PATHS = {
    "/health",
    "/.well-known/oauth-protected-resource",
    "/.well-known/openid-configuration",
    "/jwks",
    "/oauth/authorize",
    "/oauth/token",
    "/oauth/revoke",
    "/auth/login",
    "/auth/logout",
    "/auth/sso",
    "/dashboard/login",
}


def auth_middleware(inner_app):
    """
    ASGI middleware that enforces auth on protected paths.

    Session auth (always active):
      /dashboard* → session required; no session → 302 to /dashboard/login?next=...
      /dashboard/api/* → session required; no session → 401 JSON

    Bearer JWT auth (only when AUTH_ISSUER_URL is set):
      All other paths → Bearer token required.
    """
    async def middleware(scope, receive, send):
        if scope["type"] != "http":
            return await inner_app(scope, receive, send)

        # Bypass all auth for localhost connections (CC subprocesses on the same host)
        client = scope.get("client")
        if client and client[0] in ("127.0.0.1", "::1"):
            return await inner_app(scope, receive, send)

        path = scope.get("path", "")

        # Serve protected resource metadata (unauthenticated)
        if path == "/.well-known/oauth-protected-resource":
            await _send_json(send, 200, _protected_resource_metadata())
            return

        # ── Session auth for /dashboard* (except /dashboard/api/*) ──────────
        # /dashboard/login is public; everything else requires a session.
        # /dashboard/api/* is handled separately below.
        if path.startswith("/dashboard") and path != "/dashboard/login" and not path.startswith("/dashboard/api/"):
            user = await get_session_user(scope)
            if user is None:
                if AUTH_MODE == "saas":
                    location = _saas_redirect_url(scope)
                else:
                    # Build next= param: path + query string
                    qs = scope.get("query_string", b"").decode("utf-8", errors="replace")
                    next_path = path + ("?" + qs if qs else "")
                    location = "/dashboard/login?next=" + quote(next_path, safe="")
                await send({
                    "type": "http.response.start",
                    "status": 302,
                    "headers": [[b"location", location.encode()]],
                })
                await send({"type": "http.response.body", "body": b""})
                return
            scope["session_user"] = user
            return await inner_app(scope, receive, send)

        # ── Session auth for /dashboard/api/* ──────────────────────────────
        if path.startswith("/dashboard/api/"):
            user = await get_session_user(scope)
            if user is None:
                if AUTH_MODE == "saas":
                    location = _saas_redirect_url(scope)
                    await send({
                        "type": "http.response.start",
                        "status": 302,
                        "headers": [[b"location", location.encode()]],
                    })
                    await send({"type": "http.response.body", "body": b""})
                else:
                    await _send_json(send, 401, {"error": "authentication_required"})
                return
            scope["session_user"] = user
            return await inner_app(scope, receive, send)

        # ── Internal API — handles its own Bearer token auth ────────────────
        if path.startswith("/internal/"):
            return await inner_app(scope, receive, send)

        # ── Bearer JWT for all other paths (/mcp, OAuth, etc.) ─────────────
        # Always active: self-issued mode uses local RSA key, external mode
        # fetches JWKS from the configured AUTH_ISSUER_URL.

        # Skip auth for explicitly unprotected paths
        if path in UNPROTECTED_PATHS:
            return await inner_app(scope, receive, send)

        # Redirect unknown paths to /dashboard — only /mcp and /mcp/worker need JWT
        if path not in ("/mcp", "/mcp/worker"):
            await send({
                "type": "http.response.start",
                "status": 302,
                "headers": [[b"location", b"/dashboard"]],
            })
            await send({"type": "http.response.body", "body": b""})
            return

        # HEAD on /mcp returns protocol version (unauthenticated, for discovery)
        method = scope.get("method", "")
        if path == "/mcp" and method == "HEAD":
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"text/plain"],
                    [b"mcp-protocol-version", b"2025-06-18"],
                    _www_authenticate_header(),
                ],
            })
            await send({"type": "http.response.body", "body": b""})
            return

        # Extract Bearer token
        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode()

        if not auth_header.startswith("Bearer "):
            await _send_json(
                send, 401,
                {"error": "invalid_token", "error_description": "Missing or malformed Authorization header"},
                extra_headers=[_www_authenticate_header("invalid_token", "Missing or malformed Authorization header")],
            )
            return

        token = auth_header[7:]  # Strip "Bearer "
        claims = await verify_token(token)

        if claims is None:
            await _send_json(
                send, 401,
                {"error": "invalid_token", "error_description": "Token validation failed"},
                extra_headers=[_www_authenticate_header("invalid_token", "Token validation failed")],
            )
            return

        # Attach claims to scope for downstream use
        scope["auth_claims"] = claims
        return await inner_app(scope, receive, send)

    return middleware
