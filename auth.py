"""Backward-compatible shim — auth moved to switchboard.auth.middleware."""
from switchboard.auth.middleware import *  # noqa: F401, F403
from switchboard.auth.middleware import auth_middleware, is_auth_enabled, verify_token  # noqa: F401
