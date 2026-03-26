"""
OAuth 2.1 resource server middleware for Switchboard.

When AUTH_ISSUER_URL is set, all requests to protected paths require a valid
Bearer token issued by the configured authorization server (Authelia).
Tokens are validated against the issuer's JWKS endpoint.

When AUTH_ISSUER_URL is unset, auth is disabled (local dev mode).
"""

import json
import logging
import time
from typing import Any

import httpx
import jwt

logger = logging.getLogger("switchboard.auth")

# ── Configuration ──────────────────────────────────────────────────────────

from switchboard.config.settings import (
    AUTH_ISSUER_URL,
    AUTH_AUDIENCE,
    AUTH_REQUIRED_SCOPES,
    RESOURCE_URL,
    OAUTH_BASE_URL,
)


def is_auth_enabled() -> bool:
    return bool(AUTH_ISSUER_URL)


# ── JWKS cache ─────────────────────────────────────────────────────────────

_jwks_cache: dict[str, Any] = {}
_jwks_cache_time: float = 0
_JWKS_CACHE_TTL = 3600  # 1 hour


async def _get_jwks() -> dict[str, Any]:
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


async def verify_token(token: str) -> dict | None:
    """Validate a JWT against the issuer's JWKS. Returns claims dict or None."""
    try:
        jwks_data = await _get_jwks()
        # Get the signing key from JWKS
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        key = None
        for jwk in jwks_data.get("keys", []):
            if jwk.get("kid") == kid:
                key = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
                break

        if key is None:
            # Key not found — maybe rotated, clear cache and retry once
            global _jwks_cache_time
            _jwks_cache_time = 0
            jwks_data = await _get_jwks()
            for jwk in jwks_data.get("keys", []):
                if jwk.get("kid") == kid:
                    key = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
                    break

        if key is None:
            return None

        # Log what we're about to validate against
        expected_issuer = AUTH_ISSUER_URL.rstrip("/")
        unverified_claims = jwt.decode(token, options={"verify_signature": False})
        logger.debug(f"Token iss={unverified_claims.get('iss')!r}, expected={expected_issuer!r}")
        logger.debug(f"Token aud={unverified_claims.get('aud')!r}, verify_aud={bool(AUTH_AUDIENCE)}")
        logger.debug(f"Token alg={unverified_header.get('alg')!r}, kid={kid!r}")

        decode_opts = {
            # Must match algorithms the OIDC issuer (Authelia) actually uses
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

    When OAUTH_BASE_URL is set, points to self as the authorization server.
    Otherwise falls back to external AUTH_ISSUER_URL (Authelia).
    """
    if OAUTH_BASE_URL:
        base = OAUTH_BASE_URL.rstrip("/")
        return {
            "resource": RESOURCE_URL or base,
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
        }
    meta = {
        "resource": RESOURCE_URL or AUTH_ISSUER_URL,
        "authorization_servers": [AUTH_ISSUER_URL.rstrip("/")],
        "bearer_methods_supported": ["header"],
    }
    if AUTH_REQUIRED_SCOPES:
        meta["scopes_supported"] = AUTH_REQUIRED_SCOPES
    return meta


# ── Middleware ──────────────────────────────────────────────────────────────

UNPROTECTED_PATHS = {
    "/health",
    "/.well-known/oauth-protected-resource",
    "/.well-known/openid-configuration",
    "/jwks",
    "/oauth/authorize",
    "/oauth/token",
    "/oauth/revoke",
}


def auth_middleware(inner_app):
    """
    ASGI middleware that enforces Bearer token auth on protected paths.
    No-op when AUTH_ISSUER_URL is not set.
    """
    if not is_auth_enabled():
        return inner_app

    async def middleware(scope, receive, send):
        if scope["type"] != "http":
            return await inner_app(scope, receive, send)

        # Bypass auth for localhost connections (CC subprocesses on the same host)
        client = scope.get("client")
        if client and client[0] in ("127.0.0.1", "::1"):
            return await inner_app(scope, receive, send)

        path = scope.get("path", "")

        # Serve protected resource metadata (unauthenticated)
        if path == "/.well-known/oauth-protected-resource":
            await _send_json(send, 200, _protected_resource_metadata())
            return

        # Dashboard paths bypass OAuth — Caddy handles basic auth (htpasswd)
        # for /dashboard routes before requests reach this middleware.
        if path.startswith("/dashboard") or path.startswith("/foreman"):
            return await inner_app(scope, receive, send)

        # Skip auth for unprotected paths
        if path in UNPROTECTED_PATHS:
            return await inner_app(scope, receive, send)

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
