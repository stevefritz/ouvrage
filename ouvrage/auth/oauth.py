"""OAuth 2.0 Authorization Server for Ouvrage.

Provides OIDC discovery, JWKS, authorization code flow with PKCE S256,
RS256 JWT access tokens, opaque refresh tokens, and RFC 7009 revocation.

Uses authlib for JWT signing and cryptography for RSA key management.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from urllib.parse import urlencode, parse_qs

from authlib.jose import jwt as authlib_jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ouvrage.config.settings import OAUTH_BASE_URL, OAUTH_RSA_KEY_PATH
from ouvrage.crypto import encrypt_value, decrypt_value
from ouvrage.db.connection import get_db

logger = logging.getLogger("ouvrage.auth.oauth")

# ── Constants ─────────────────────────────────────────────────────────────

RSA_KID = "ouvrage-1"
ACCESS_TOKEN_TTL = 3600  # 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days
AUTH_CODE_TTL = 600  # 10 minutes

# All scopes we accept (Claude.ai compat — accept everything it asks for)
SUPPORTED_SCOPES = [
    "openid", "profile", "email", "offline_access",
    "address", "phone", "groups", "claudeai",
]

DEFAULT_CLIENT_REDIRECT_URIS = [
    "https://claude.ai/oauth/callback",
    "https://claude.ai/api/auth/oauth/callback",
    "https://claude.ai/api/mcp/auth_callback",
]

# ── RSA Key Management ────────────────────────────────────────────────────

_rsa_private_key = None
_rsa_public_jwk = None


def _ensure_rsa_key():
    """Load or generate RSA keypair. Called once at startup."""
    global _rsa_private_key, _rsa_public_jwk

    key_path = OAUTH_RSA_KEY_PATH

    if os.path.exists(key_path):
        with open(key_path, "rb") as f:
            _rsa_private_key = serialization.load_pem_private_key(f.read(), password=None)
        logger.info("Loaded RSA key from %s", key_path)
    else:
        _rsa_private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)
        with open(key_path, "wb") as f:
            f.write(_rsa_private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ))
        os.chmod(key_path, 0o600)
        logger.info("Generated new RSA key at %s", key_path)

    # Build public JWK for JWKS endpoint
    pub = _rsa_private_key.public_key()
    pub_numbers = pub.public_numbers()

    def _int_to_base64url(n, length=None):
        b = n.to_bytes((n.bit_length() + 7) // 8, byteorder="big")
        if length and len(b) < length:
            b = b"\x00" * (length - len(b)) + b
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    _rsa_public_jwk = {
        "kty": "RSA",
        "kid": RSA_KID,
        "use": "sig",
        "alg": "RS256",
        "n": _int_to_base64url(pub_numbers.n),
        "e": _int_to_base64url(pub_numbers.e),
    }


def init_oauth_keys():
    """Initialize RSA keys. Call during server startup."""
    _ensure_rsa_key()


def get_jwks() -> dict:
    """Return JWKS document with the public key."""
    if _rsa_public_jwk is None:
        _ensure_rsa_key()
    return {"keys": [_rsa_public_jwk]}


def _get_base_url() -> str:
    """Get the OAuth base URL from config or fall back to localhost."""
    return (OAUTH_BASE_URL or "http://localhost:8100").rstrip("/")


# ── OIDC Discovery ────────────────────────────────────────────────────────

def get_openid_configuration() -> dict:
    """Return OpenID Connect discovery document."""
    base = _get_base_url()
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "jwks_uri": f"{base}/jwks",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "scopes_supported": SUPPORTED_SCOPES,
        "code_challenge_methods_supported": ["S256"],
        "claims_supported": ["sub", "iss", "aud", "exp", "iat", "email", "name", "scope"],
    }


# ── Client Management ─────────────────────────────────────────────────────

async def get_client(client_id: str) -> dict | None:
    """Fetch an OAuth client by client_id."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id,)
        )
        if not rows:
            return None
        row = dict(rows[0])
        # Parse JSON fields
        for field in ("redirect_uris", "grant_types", "scopes"):
            if isinstance(row[field], str):
                row[field] = json.loads(row[field])
        return row


async def validate_client_secret(client_id: str, client_secret: str) -> bool:
    """Validate a client's secret against the encrypted stored value."""
    client = await get_client(client_id)
    if not client or not client.get("client_secret_encrypted"):
        return False
    try:
        stored_secret = decrypt_value(client["client_secret_encrypted"])
        return secrets.compare_digest(stored_secret, client_secret)
    except Exception:
        return False


# ── PKCE ──────────────────────────────────────────────────────────────────

def verify_pkce(code_verifier: str, code_challenge: str, method: str = "S256") -> bool:
    """Verify PKCE code_verifier against stored code_challenge."""
    if method != "S256":
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(computed, code_challenge)


# ── Authorization Code ────────────────────────────────────────────────────

async def create_authorization_code(
    client_id: str,
    user_id: int,
    redirect_uri: str,
    scope: str,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
) -> str:
    """Create and store an authorization code. Returns the code string."""
    code = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + AUTH_CODE_TTL

    async with get_db() as db:
        await db.execute(
            """INSERT INTO oauth_authorization_codes
               (code, client_id, user_id, redirect_uri, scope,
                code_challenge, code_challenge_method, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, client_id, user_id, redirect_uri, scope,
             code_challenge, code_challenge_method, expires_at),
        )
        await db.commit()
    return code


async def consume_authorization_code(code: str) -> dict | None:
    """Fetch and delete an authorization code. Returns code data or None if invalid/expired."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM oauth_authorization_codes WHERE code = ?", (code,)
        )
        if not rows:
            return None

        code_data = dict(rows[0])

        # Delete the code (single-use)
        await db.execute(
            "DELETE FROM oauth_authorization_codes WHERE code = ?", (code,)
        )
        await db.commit()

        # Check expiry
        if code_data["expires_at"] < int(time.time()):
            return None

        return code_data


# ── Token Issuance ────────────────────────────────────────────────────────

def _create_access_token_jwt(
    user_id: int,
    client_id: str,
    scope: str,
    email: str | None = None,
    name: str | None = None,
) -> tuple[str, str, int]:
    """Create an RS256 JWT access token. Returns (jwt_string, jti, expires_at)."""
    if _rsa_private_key is None:
        _ensure_rsa_key()

    now = int(time.time())
    exp = now + ACCESS_TOKEN_TTL
    jti = str(uuid.uuid4())

    header = {"alg": "RS256", "kid": RSA_KID}
    payload = {
        "iss": _get_base_url(),
        "sub": str(user_id),
        "aud": client_id,
        "exp": exp,
        "iat": now,
        "jti": jti,
        "scope": scope,
    }
    if email:
        payload["email"] = email
    if name:
        payload["name"] = name

    # Serialize private key to PEM for authlib
    pem = _rsa_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    token = authlib_jwt.encode(header, payload, pem)
    # authlib returns bytes
    if isinstance(token, bytes):
        token = token.decode("utf-8")

    return token, jti, exp


async def issue_tokens(
    client_id: str,
    user_id: int,
    scope: str,
    email: str | None = None,
    name: str | None = None,
) -> dict:
    """Issue access token (JWT) + refresh token (opaque). Stores in DB."""
    access_token, jti, access_exp = _create_access_token_jwt(
        user_id, client_id, scope, email, name
    )
    refresh_token = secrets.token_urlsafe(48)
    now = int(time.time())
    refresh_exp = now + REFRESH_TOKEN_TTL

    async with get_db() as db:
        await db.execute(
            """INSERT INTO oauth_tokens
               (client_id, user_id, token_type, access_token_jti,
                refresh_token, scope, issued_at,
                access_token_expires_at, refresh_token_expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (client_id, user_id, "Bearer", jti,
             refresh_token, scope, now, access_exp, refresh_exp),
        )
        await db.commit()

    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL,
        "refresh_token": refresh_token,
        "scope": scope,
    }


async def refresh_access_token(refresh_token: str, client_id: str) -> dict | None:
    """Exchange a refresh token for new tokens (rotation). Returns token response or None."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT * FROM oauth_tokens
               WHERE refresh_token = ? AND client_id = ? AND revoked = 0""",
            (refresh_token, client_id),
        )
        if not rows:
            return None

        token_row = dict(rows[0])

        # Check refresh token expiry
        if token_row["refresh_token_expires_at"] < int(time.time()):
            return None

        # Revoke old token (rotation)
        await db.execute(
            "UPDATE oauth_tokens SET revoked = 1 WHERE id = ?",
            (token_row["id"],),
        )
        await db.commit()

    # Look up user info for JWT claims
    user = await _get_user(token_row["user_id"])
    email = user.get("email") if user else None
    name = user.get("name") if user else None

    # Issue new token pair
    return await issue_tokens(
        client_id=client_id,
        user_id=token_row["user_id"],
        scope=token_row["scope"],
        email=email,
        name=name,
    )


# ── Token Revocation (RFC 7009) ──────────────────────────────────────────

async def revoke_token(token: str, token_type_hint: str | None = None) -> bool:
    """Revoke a token (access or refresh). Returns True if found and revoked."""
    async with get_db() as db:
        # Try refresh token first (or if hinted)
        if token_type_hint != "access_token":
            result = await db.execute(
                "UPDATE oauth_tokens SET revoked = 1 WHERE refresh_token = ? AND revoked = 0",
                (token,),
            )
            if result.rowcount > 0:
                await db.commit()
                return True

        # Try as access token JTI — but the token param is the full JWT, so
        # we need to extract the JTI. For simplicity, also try direct JTI match.
        # In practice, clients send the full access token string.
        try:
            # Decode without verification to get JTI
            import jwt as pyjwt
            claims = pyjwt.decode(token, options={"verify_signature": False})
            jti = claims.get("jti")
            if jti:
                result = await db.execute(
                    "UPDATE oauth_tokens SET revoked = 1 WHERE access_token_jti = ? AND revoked = 0",
                    (jti,),
                )
                if result.rowcount > 0:
                    await db.commit()
                    return True
        except Exception:
            pass

        await db.commit()
    return False


# ── User Lookup ───────────────────────────────────────────────────────────

async def _get_user(user_id: int) -> dict | None:
    """Fetch user by ID."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT id, email, name FROM users WHERE id = ?", (user_id,)
        )
        return dict(rows[0]) if rows else None


# ── ASGI Request Helpers ──────────────────────────────────────────────────

async def _read_body(receive) -> bytes:
    """Read full request body from ASGI receive channel."""
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    return body


async def _send_json(send, status: int, body: dict, extra_headers: list | None = None):
    """Send a JSON response."""
    headers = [
        [b"content-type", b"application/json"],
        [b"cache-control", b"no-store"],
        [b"pragma", b"no-cache"],
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


async def _send_redirect(send, url: str, status: int = 302):
    """Send an HTTP redirect."""
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [[b"location", url.encode()]],
    })
    await send({"type": "http.response.body", "body": b""})


# ── ASGI Endpoint Handlers ───────────────────────────────────────────────

async def handle_openid_configuration(scope, receive, send):
    """GET /.well-known/openid-configuration"""
    await _send_json(send, 200, get_openid_configuration())


async def handle_jwks(scope, receive, send):
    """GET /jwks"""
    await _send_json(send, 200, get_jwks())


async def handle_authorize(scope, receive, send):
    """GET /oauth/authorize — Authorization code flow entry point.

    Returns 401 with login_required error if no session.
    Otherwise generates an auth code and redirects (implicit consent).
    """
    # Parse query string
    qs = scope.get("query_string", b"").decode()
    params = parse_qs(qs, keep_blank_values=True)

    client_id = params.get("client_id", [None])[0]
    redirect_uri = params.get("redirect_uri", [None])[0]
    response_type = params.get("response_type", [None])[0]
    scope_param = params.get("scope", [""])[0]
    state = params.get("state", [None])[0]
    code_challenge = params.get("code_challenge", [None])[0]
    code_challenge_method = params.get("code_challenge_method", [None])[0]

    # Validate response_type
    if response_type != "code":
        await _send_json(send, 400, {
            "error": "unsupported_response_type",
            "error_description": "Only response_type=code is supported",
        })
        return

    # Validate client
    client = await get_client(client_id)
    if not client:
        await _send_json(send, 400, {
            "error": "invalid_client",
            "error_description": "Unknown client_id",
        })
        return

    # Validate redirect_uri — allow any localhost port per RFC 8252 (native apps)
    def _is_valid_redirect(uri, allowed):
        if uri in allowed:
            return True
        # http://localhost:{any_port}/callback is always allowed for native CLI auth
        if uri and uri.startswith("http://localhost:") and "/callback" in uri:
            return True
        return False

    if not _is_valid_redirect(redirect_uri, client["redirect_uris"]):
        await _send_json(send, 400, {
            "error": "invalid_request",
            "error_description": "Invalid redirect_uri",
        })
        return

    # Check for session (user_id in scope, set by session middleware)
    user_id = scope.get("oauth_user_id")
    if not user_id:
        # No session — redirect to login page with next= pointing back here
        from urllib.parse import quote
        # Use relative path so _safe_next_url accepts it (no scheme/netloc)
        authorize_path = "/oauth/authorize"
        if qs:
            authorize_path = f"{authorize_path}?{qs}"
        login_url = f"/dashboard/login?next={quote(authorize_path, safe='')}"
        await _send_redirect(send, login_url)
        return

    # Implicit consent — generate code and redirect immediately
    code = await create_authorization_code(
        client_id=client_id,
        user_id=user_id,
        redirect_uri=redirect_uri,
        scope=scope_param,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )

    # Build redirect URL with code and state
    redirect_params = {"code": code}
    if state:
        redirect_params["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    redirect_url = f"{redirect_uri}{separator}{urlencode(redirect_params)}"

    await _send_redirect(send, redirect_url)


async def handle_token(scope, receive, send):
    """POST /oauth/token — Token exchange endpoint.

    Supports grant_type=authorization_code (with PKCE) and grant_type=refresh_token.
    Auth method: client_secret_post (credentials in request body).
    """
    body = await _read_body(receive)
    params = parse_qs(body.decode(), keep_blank_values=True)

    grant_type = params.get("grant_type", [None])[0]
    client_id = params.get("client_id", [None])[0]
    client_secret = params.get("client_secret", [None])[0]

    # Validate client credentials (client_secret_post)
    if not client_id or not client_secret:
        await _send_json(send, 401, {
            "error": "invalid_client",
            "error_description": "client_id and client_secret required",
        })
        return

    if not await validate_client_secret(client_id, client_secret):
        await _send_json(send, 401, {
            "error": "invalid_client",
            "error_description": "Invalid client credentials",
        })
        return

    if grant_type == "authorization_code":
        await _handle_auth_code_grant(params, client_id, send)
    elif grant_type == "refresh_token":
        await _handle_refresh_grant(params, client_id, send)
    else:
        await _send_json(send, 400, {
            "error": "unsupported_grant_type",
            "error_description": f"Unsupported grant_type: {grant_type}",
        })


async def _handle_auth_code_grant(params: dict, client_id: str, send):
    """Handle authorization_code grant type."""
    code = params.get("code", [None])[0]
    redirect_uri = params.get("redirect_uri", [None])[0]
    code_verifier = params.get("code_verifier", [None])[0]

    if not code:
        await _send_json(send, 400, {
            "error": "invalid_request",
            "error_description": "Missing authorization code",
        })
        return

    code_data = await consume_authorization_code(code)
    if not code_data:
        await _send_json(send, 400, {
            "error": "invalid_grant",
            "error_description": "Invalid or expired authorization code",
        })
        return

    # Verify client_id matches
    if code_data["client_id"] != client_id:
        await _send_json(send, 400, {
            "error": "invalid_grant",
            "error_description": "Authorization code was issued to a different client",
        })
        return

    # Verify redirect_uri matches
    if redirect_uri and code_data["redirect_uri"] != redirect_uri:
        await _send_json(send, 400, {
            "error": "invalid_grant",
            "error_description": "redirect_uri mismatch",
        })
        return

    # Verify PKCE
    if code_data.get("code_challenge"):
        if not code_verifier:
            await _send_json(send, 400, {
                "error": "invalid_request",
                "error_description": "code_verifier required for PKCE",
            })
            return
        if not verify_pkce(code_verifier, code_data["code_challenge"],
                          code_data.get("code_challenge_method", "S256")):
            await _send_json(send, 400, {
                "error": "invalid_grant",
                "error_description": "PKCE verification failed",
            })
            return

    # Look up user for JWT claims
    user = await _get_user(code_data["user_id"])
    email = user.get("email") if user else None
    name = user.get("name") if user else None

    # Issue tokens
    token_response = await issue_tokens(
        client_id=client_id,
        user_id=code_data["user_id"],
        scope=code_data["scope"],
        email=email,
        name=name,
    )

    await _send_json(send, 200, token_response)


async def _handle_refresh_grant(params: dict, client_id: str, send):
    """Handle refresh_token grant type."""
    refresh_token = params.get("refresh_token", [None])[0]
    if not refresh_token:
        await _send_json(send, 400, {
            "error": "invalid_request",
            "error_description": "Missing refresh_token",
        })
        return

    token_response = await refresh_access_token(refresh_token, client_id)
    if not token_response:
        await _send_json(send, 400, {
            "error": "invalid_grant",
            "error_description": "Invalid or expired refresh token",
        })
        return

    await _send_json(send, 200, token_response)


async def handle_revoke(scope, receive, send):
    """POST /oauth/revoke — RFC 7009 token revocation.

    Always returns 200 per spec (even if token not found).
    """
    body = await _read_body(receive)
    params = parse_qs(body.decode(), keep_blank_values=True)

    token = params.get("token", [None])[0]
    token_type_hint = params.get("token_type_hint", [None])[0]

    if not token:
        await _send_json(send, 400, {
            "error": "invalid_request",
            "error_description": "Missing token parameter",
        })
        return

    await revoke_token(token, token_type_hint)

    # RFC 7009: always return 200, even if token was not found
    await _send_json(send, 200, {})


# ── Client Seeding ────────────────────────────────────────────────────────

async def seed_default_client():
    """Seed the claude-mcp OAuth client if it doesn't exist.

    Client secret is either from OAUTH_CLIENT_SECRET env var or auto-generated.
    Stored encrypted via Fernet.
    """
    from ouvrage.config.settings import OAUTH_CLIENT_SECRET

    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT client_id FROM oauth_clients WHERE client_id = ?",
            ("claude-mcp",),
        )
        if rows:
            return  # Already seeded

        # Generate or use configured secret
        client_secret = OAUTH_CLIENT_SECRET or secrets.token_urlsafe(32)
        encrypted_secret = encrypt_value(client_secret)

        await db.execute(
            """INSERT INTO oauth_clients
               (client_id, client_name, client_secret_encrypted,
                redirect_uris, grant_types, scopes,
                token_endpoint_auth_method, consent_mode)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "claude-mcp",
                "Claude MCP Client",
                encrypted_secret,
                json.dumps(DEFAULT_CLIENT_REDIRECT_URIS),
                json.dumps(["authorization_code", "refresh_token"]),
                json.dumps(SUPPORTED_SCOPES),
                "client_secret_post",
                "implicit",
            ),
        )
        await db.commit()

        if not OAUTH_CLIENT_SECRET:
            logger.info("Seeded claude-mcp client with auto-generated secret")
        else:
            logger.info("Seeded claude-mcp client with configured secret")
