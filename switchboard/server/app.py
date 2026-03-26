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
from switchboard.dashboard import api as dashboard_api
import switchboard.db as db
import switchboard.dispatch as tasks

from switchboard.server.tools import TOOLS
from switchboard.server.dispatch import _dispatch_tool

log = logging.getLogger("switchboard.server")

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
    """Serve static files from dashboard/ (shared assets used by Foreman)."""
    path = scope["path"]
    # Strip /dashboard prefix to get file path
    file_path = path[len("/dashboard"):].lstrip("/")
    if not file_path:
        await send({"type": "http.response.start", "status": 404, "headers": [[b"content-type", b"text/plain"]]})
        await send({"type": "http.response.body", "body": b"Not Found"})
        return

    full_path = os.path.join(_DASHBOARD_DIR, file_path)

    # Security: prevent path traversal
    full_path = os.path.realpath(full_path)
    if not full_path.startswith(os.path.realpath(_DASHBOARD_DIR)):
        await send({"type": "http.response.start", "status": 403, "headers": []})
        await send({"type": "http.response.body", "body": b"Forbidden"})
        return

    if not os.path.isfile(full_path):
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b"Not Found"})
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
    if path in ("/foreman", "/foreman.html", "/foreman/"):
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

async def main():
    await db.init_db()

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
            await session_manager.handle_request(scope, receive, send)
        elif path.startswith("/dashboard/api/"):
            await dashboard_api.handle_request(scope, receive, send)
        elif path.startswith("/dashboard"):
            await _serve_dashboard(scope, send)
        elif path.startswith("/foreman"):
            await _serve_foreman(scope, send)
        else:
            await send({"type": "http.response.start", "status": 404, "headers": [[b"content-type", b"text/plain"]]})
            await send({"type": "http.response.body", "body": b"Not Found"})

    # Wrap with OAuth middleware (no-op if AUTH_ISSUER_URL is unset)
    protected_app = auth.auth_middleware(app)

    port = int(os.environ.get("SWITCHBOARD_PORT", "8100"))

    if auth.is_auth_enabled():
        print(f"OAuth enabled — issuer: {auth.AUTH_ISSUER_URL}")
    else:
        print("OAuth disabled — no AUTH_ISSUER_URL set (local dev mode)")

    config = uvicorn.Config(protected_app, host="0.0.0.0", port=port, log_level="info")
    srv = uvicorn.Server(config)
    await srv.serve()
