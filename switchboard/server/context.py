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

# True if this request came in via /mcp/worker (CC task session, not a human user)
_REQUEST_IS_WORKER: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_REQUEST_IS_WORKER", default=False
)

# Base URL for this instance (e.g. "https://switchboard.example.dev"), used to
# build canonical task URLs. Set from OAUTH_BASE_URL config or request Host header.
_REQUEST_BASE_URL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_REQUEST_BASE_URL", default=None
)


def get_request_user_id() -> int | None:
    """Return the resolved user_id for the current request, or None."""
    return _REQUEST_USER_ID.get()


def get_request_is_token_auth() -> bool:
    """Return True if the current request authenticated via a valid API token."""
    return _REQUEST_IS_TOKEN_AUTH.get()


def get_request_is_worker() -> bool:
    """Return True if the current request came via the /mcp/worker endpoint."""
    return _REQUEST_IS_WORKER.get()


def get_request_base_url() -> str | None:
    """Return the base URL for the current request (e.g. 'https://switchboard.example.dev')."""
    return _REQUEST_BASE_URL.get()


def set_request_context(
    user_id: int | None,
    is_token_auth: bool,
    is_worker: bool = False,
    base_url: str | None = None,
) -> None:
    """Set all context vars for the current request."""
    _REQUEST_USER_ID.set(user_id)
    _REQUEST_IS_TOKEN_AUTH.set(is_token_auth)
    _REQUEST_IS_WORKER.set(is_worker)
    _REQUEST_BASE_URL.set(base_url)
