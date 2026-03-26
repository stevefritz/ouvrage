"""Request-scoped context variables for the MCP handler pipeline.

Set once per request in app.py before handle_request(). Propagates automatically
to all coroutines and tasks spawned during the request via asyncio's ContextVar
inheritance.
"""
import contextvars

# user_id resolved from Bearer token or fallback to instance owner
_REQUEST_USER_ID: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "_REQUEST_USER_ID", default=None
)

# True if the user_id came from a valid API token; False if unauthenticated fallback
_REQUEST_IS_TOKEN_AUTH: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_REQUEST_IS_TOKEN_AUTH", default=False
)


def get_request_user_id() -> int | None:
    """Return the resolved user_id for the current request, or None."""
    return _REQUEST_USER_ID.get()


def get_request_is_token_auth() -> bool:
    """Return True if the current request authenticated via a valid API token."""
    return _REQUEST_IS_TOKEN_AUTH.get()


def set_request_context(user_id: int | None, is_token_auth: bool) -> None:
    """Set both context vars atomically for the current request."""
    _REQUEST_USER_ID.set(user_id)
    _REQUEST_IS_TOKEN_AUTH.set(is_token_auth)
