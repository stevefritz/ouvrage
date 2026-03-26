"""ASGI app factory, MCP server setup, middleware chain, and entry point."""

import asyncio
import json
import logging
import os

import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent

from switchboard.auth import middleware as auth
from switchboard.auth import oauth as oauth_server
from switchboard.auth import sessions as session_server
from switchboard.dashboard import api as dashboard_api
import switchboard.db as db
import switchboard.dispatch as tasks

from switchboard.server.tools import TOOLS
from switchboard.server.dispatch import _dispatch_tool
from switchboard.server.context import set_request_context

log = logging.getLogger("switchboard.server")

_SYSTEM_AUTHORS = frozenset({"dispatcher", "cc-worker", "switchboard"})


async def _resolve_mcp_user_id(scope) -> tuple[int | None, bool]:
    """Extract Bearer token from ASGI headers and resolve to a user_id.

    Returns (user_id, is_token_auth) where is_token_auth=True if a valid
    API token was presented, False if falling back to the instance owner.

    Backward compatibility: if no token is provided, fall back to the instance
    owner's user_id so single-tenant instances work without configuration.
    """
    # Extract Authorization header
    raw_token: str | None = None
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            auth_str = value.decode("utf-8", errors="replace")
            if auth_str.lower().startswith("bearer "):
                raw_token = auth_str[7:].strip()
            break

    if raw_token:
        user_id = await db.validate_api_token(raw_token)
        if user_id is not None:
            return user_id, True
        # Token provided but invalid — return None so handlers get no user
        log.warning("MCP request with invalid API token")
        return None, False

    # No token — fall back to instance owner (single-tenant mode)
    log.warning(
        "No API token provided, using instance owner. "
        "Set up tokens for proper attribution."
    )
    instance = await db.get_instance()
    if instance and instance.get("owner_user_id"):
        return instance["owner_user_id"], False
    return None, False


SERVER_INSTRUCTIONS = """\
Switchboard is a task orchestration system. It dispatches autonomous Claude Code \
workers to git repos, manages worktrees, runs test/review gates, and tracks \
everything through a dashboard.

START HERE: Call `get_context` at the beginning of every conversation. It returns \
a compact snapshot of active projects, running tasks, and recent events — enough \
to orient without reading the full guide.

CONVERSATIONS ARE YOUR MEMORY: Switchboard conversations persist across sessions. \
Use them to store specs, design decisions, research, and ongoing context. When \
starting work on a topic, search conversations first — prior context likely exists. \
Pin critical messages (specs, decisions) so they're easy to retrieve later.

TYPICAL WORKFLOW:
1. get_context → orient to current state
2. conversations(search="topic") → find prior context
3. read(conversation_id) → load relevant history
4. Then: create tasks, dispatch work, post updates
5. When tasks complete, use list_task_files + get_task_file to verify output
6. If you need the full tool reference, call get_guide

KEY CONCEPTS:
- Projects = git repos registered for task dispatch
- Components = feature groupings within a project (optional)
- Tasks = units of work dispatched to CC workers in isolated worktrees
- Conversations = persistent threads for specs, plans, Q&A — your long-term memory
- Punchlist = tracked items within a component that tasks can claim and resolve

When the user discusses a project or feature, proactively check conversations \
for existing context before starting fresh. Post summaries and decisions back \
to conversations so future sessions have continuity.\
"""

server = Server("switchboard", instructions=SERVER_INSTRUCTIONS)


@server.list_tools()
async def list_tools():
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        result = await _dispatch_tool(name, arguments)
        return [TextContent(type="text", text=json.dumps(result, separators=(",", ":"), default=str))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

_DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "dashboard")
_MIME_TYPES = {
    ".html": "text/html", ".js": "application/javascript",
    ".css": "text/css", ".json": "application/json",
    ".svg": "image/svg+xml", ".png": "image/png",
}


async def _serve_dashboard(scope, send):
    """Serve static files from dashboard/, with SPA fallback to index.html."""
    path = scope["path"]
    # Strip /dashboard prefix to get file path
    file_path = path[len("/dashboard"):].lstrip("/")
    if not file_path:
        file_path = "index.html"

    full_path = os.path.join(_DASHBOARD_DIR, file_path)

    # Security: prevent path traversal
    full_path = os.path.realpath(full_path)
    if not full_path.startswith(os.path.realpath(_DASHBOARD_DIR)):
        await send({"type": "http.response.start", "status": 403, "headers": []})
        await send({"type": "http.response.body", "body": b"Forbidden"})
        return

    # SPA fallback: if file doesn't exist, serve index.html
    if not os.path.isfile(full_path):
        full_path = os.path.join(_DASHBOARD_DIR, "index.html")

    if not os.path.isfile(full_path):
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"Dashboard not found"})
        return

    ext = os.path.splitext(full_path)[1]
    content_type = _MIME_TYPES.get(ext, "application/octet-stream")

    with open(full_path, "rb") as f:
        body = f.read()

    await send({
        "type": "http.response.start", "status": 200,
        "headers": [[b"content-type", content_type.encode()]],
    })
    await send({"type": "http.response.body", "body": body})


async def _serve_foreman(scope, send):
    """Serve foreman.html and its assets from dashboard/."""
    path = scope["path"]
    # /foreman or /foreman.html → foreman.html at app root
    # /foreman/anything → serve from dashboard/ (shared JS/CSS)
    _app_root = os.path.dirname(os.path.abspath(__file__))
    _project_root = os.path.join(_app_root, "..", "..")
    if path in ("/foreman", "/foreman.html", "/foreman/", "/foreman/login"):
        file_path = os.path.join(_project_root, "foreman.html")
    else:
        # Strip /foreman prefix, serve from dashboard dir (shared assets)
        rel = path[len("/foreman"):].lstrip("/")
        file_path = os.path.join(_DASHBOARD_DIR, rel)
        file_path = os.path.realpath(file_path)
        if not file_path.startswith(os.path.realpath(_DASHBOARD_DIR)):
            await send({"type": "http.response.start", "status": 403, "headers": []})
            await send({"type": "http.response.body", "body": b"Forbidden"})
            return

    if not os.path.isfile(file_path):
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"Not Found"})
        return

    ext = os.path.splitext(file_path)[1]
    content_type = _MIME_TYPES.get(ext, "application/octet-stream")
    with open(file_path, "rb") as f:
        body = f.read()
    await send({"type": "http.response.start", "status": 200, "headers": [[b"content-type", content_type.encode()]]})
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# ASGI app + main entry point
# ---------------------------------------------------------------------------

async def _backfill_message_chunks() -> None:
    """Background task: chunk and embed existing long messages that haven't been chunked yet."""
    total = 0
    try:
        while True:
            batch = await db.get_messages_needing_chunking(batch_size=100)
            if not batch:
                break
            for msg in batch:
                try:
                    await db.index_message_chunks(msg["id"], msg["content"])
                except Exception as e:
                    log.warning("Chunk backfill failed for message %s: %s", msg["id"], e)
                total += 1
                if total % 100 == 0:
                    log.info("Chunk backfill progress: %d messages processed", total)
        if total > 0:
            log.info("Chunk backfill complete: %d messages processed", total)
    except Exception as e:
        log.error("Chunk backfill aborted: %s", e)


async def main():
    await db.init_db()

    # Auto-migration: if owner env vars are set and no real owner exists, seed one.
    import os as _os
    _owner_email = _os.environ.get("SWITCHBOARD_OWNER_EMAIL")
    _owner_hash = _os.environ.get("SWITCHBOARD_OWNER_PASSWORD_HASH")
    if _owner_email and _owner_hash:
        from switchboard.db.users import get_user_by_email as _get_user_by_email
        _existing = await _get_user_by_email(_owner_email)
        if not _existing:
            from switchboard.migrate import run_migrate_auth as _run_migrate_auth
            log.info("Auto-migrating owner user from env vars: %s", _owner_email)
            await _run_migrate_auth(
                email=_owner_email,
                name=_os.environ.get("SWITCHBOARD_OWNER_NAME", "Owner"),
                password_hash=_owner_hash,
                slug=_os.environ.get("SWITCHBOARD_INSTANCE_SLUG", "default"),
                instance_name=_os.environ.get("SWITCHBOARD_INSTANCE_NAME", "Switchboard"),
            )

    # Initialize OAuth authorization server (RSA keys + seed default client)
    oauth_server.init_oauth_keys()
    await oauth_server.seed_default_client()

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=False,
    )

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            # Handle ASGI lifespan events for session manager
            message = await receive()
            if message["type"] == "lifespan.startup":
                ctx = session_manager.run()
                await ctx.__aenter__()
                scope["state"] = {"session_manager_ctx": ctx}
                await send({"type": "lifespan.startup.complete"})
                # Recover tasks orphaned by previous shutdown
                asyncio.create_task(tasks.recover_orphaned_tasks())
                # Start stall detection background loop
                asyncio.create_task(tasks.check_stalled_tasks())
                # Backfill message chunks for existing long messages
                asyncio.create_task(_backfill_message_chunks())
                message = await receive()
                if message["type"] == "lifespan.shutdown":
                    # Mark all working tasks for recovery before event loop dies
                    await tasks.mark_working_for_recovery()
                    await ctx.__aexit__(None, None, None)
                    await send({"type": "lifespan.shutdown.complete"})
            return

        if scope["type"] != "http":
            return

        path = scope["path"]
        method = scope.get("method", "")

        if path == "/health" and method == "GET":
            await send({"type": "http.response.start", "status": 200, "headers": [[b"content-type", b"text/plain"]]})
            await send({"type": "http.response.body", "body": b"Switchboard OK"})
        elif path == "/mcp":
            user_id, is_token_auth = await _resolve_mcp_user_id(scope)
            set_request_context(user_id, is_token_auth)
            await session_manager.handle_request(scope, receive, send)
        elif path == "/mcp/worker":
            # Worker endpoint — trust-based, no token auth required.
            # Sets is_worker=True so handlers can enforce field-level restrictions.
            # user_id is None; attribution comes from the task's dispatched_by.
            set_request_context(user_id=None, is_token_auth=False, is_worker=True)
            await session_manager.handle_request(scope, receive, send)
        elif path.startswith("/dashboard/api/"):
            await dashboard_api.handle_request(scope, receive, send)
        elif path.startswith("/dashboard"):
            await _serve_dashboard(scope, send)
        elif path.startswith("/foreman"):
            await _serve_foreman(scope, send)
        # OAuth authorization server endpoints
        elif path == "/.well-known/openid-configuration" and method == "GET":
            await oauth_server.handle_openid_configuration(scope, receive, send)
        elif path == "/jwks" and method == "GET":
            await oauth_server.handle_jwks(scope, receive, send)
        elif path == "/auth/login" and method == "POST":
            await session_server.handle_login(scope, receive, send)
        elif path == "/auth/logout" and method == "POST":
            await session_server.handle_logout(scope, receive, send)
        elif path == "/oauth/authorize" and method == "GET":
            # Inject oauth_user_id from session before authorize handler runs
            user = await session_server.get_session_user(scope)
            if user:
                scope["oauth_user_id"] = user["id"]
            await oauth_server.handle_authorize(scope, receive, send)
        elif path == "/oauth/token" and method == "POST":
            await oauth_server.handle_token(scope, receive, send)
        elif path == "/oauth/revoke" and method == "POST":
            await oauth_server.handle_revoke(scope, receive, send)
        else:
            await send({"type": "http.response.start", "status": 404, "headers": [[b"content-type", b"text/plain"]]})
            await send({"type": "http.response.body", "body": b"Not Found"})

    # Wrap with auth middleware (always active — self-issued or external)
    protected_app = auth.auth_middleware(app)

    port = int(os.environ.get("SWITCHBOARD_PORT", "8100"))

    from switchboard.auth.middleware import _is_self_issuer
    if _is_self_issuer():
        from switchboard.auth.middleware import _get_self_base_url
        print(f"OAuth enabled — self-issued JWTs (issuer: {_get_self_base_url()})")
    else:
        print(f"OAuth enabled — external issuer: {auth.AUTH_ISSUER_URL}")

    config = uvicorn.Config(protected_app, host="0.0.0.0", port=port, log_level="info")
    srv = uvicorn.Server(config)
    await srv.serve()
