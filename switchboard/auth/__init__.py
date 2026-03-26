"""switchboard.auth — OAuth 2.1 ASGI middleware + OAuth 2.0 authorization server."""

from switchboard.auth.middleware import auth_middleware, is_auth_enabled, verify_token
from switchboard.auth.oauth import (
    init_oauth_keys,
    seed_default_client,
    handle_openid_configuration,
    handle_jwks,
    handle_authorize,
    handle_token,
    handle_revoke,
)

__all__ = [
    "auth_middleware", "is_auth_enabled", "verify_token",
    "init_oauth_keys", "seed_default_client",
    "handle_openid_configuration", "handle_jwks",
    "handle_authorize", "handle_token", "handle_revoke",
]
