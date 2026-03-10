import asyncio
import json
import os

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool, TextContent

import auth
import database as db
import tasks

server = Server("switchboard")

# ---------------------------------------------------------------------------
# Conversation Tools (unchanged)
# ---------------------------------------------------------------------------

CONVERSATION_TOOLS = [
    Tool(
        name="board",
        description="Show active conversations across projects. The main dashboard view.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Filter to one project key"},
                "include_archived": {"type": "boolean", "description": "Include archived conversations", "default": False},
            },
        },
    ),
    Tool(
        name="create_conversation",
        description="Start a new conversation on the switchboard.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Slug ID, e.g. carrier-oversized-rates"},
                "project": {"type": "string", "description": "Project key, e.g. ap-carrier"},
                "goal": {"type": "string", "description": "One-liner purpose of this conversation"},
                "author": {"type": "string", "description": "Author for optional initial message"},
                "content": {"type": "string", "description": "Content for optional initial message"},
                "type": {"type": "string", "description": "Type for optional initial message"},
                "title": {"type": "string", "description": "Title for optional initial message"},
            },
            "required": ["id", "project", "goal"],
        },
    ),
    Tool(
        name="post",
        description="Post a message to a conversation. Anyone, anytime.",
        inputSchema={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Which conversation to post to"},
                "author": {"type": "string", "description": "Who is posting, e.g. claude-ai, claude-code, or a human name"},
                "content": {"type": "string", "description": "Full markdown body"},
                "type": {"type": "string", "description": "Optional: spec, plan, question, answer, note, review, status"},
                "title": {"type": "string", "description": "Optional short subject line"},
                "pinned": {"type": "boolean", "description": "Pin this message (auto-unpins previous)", "default": False},
            },
            "required": ["conversation_id", "author", "content"],
        },
    ),
    Tool(
        name="read",
        description="Get messages from a conversation. Pinned message always shown at top. Returns a cursor for polling — pass it back as 'after' to get only new messages.",
        inputSchema={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Which conversation to read"},
                "after": {"type": "integer", "description": "Cursor: return only messages with id > this value. Use the cursor from a previous read response."},
                "last_n": {"type": "integer", "description": "Return only the N most recent messages"},
                "since": {"type": "string", "description": "ISO timestamp, return messages after this time"},
                "author": {"type": "string", "description": "Filter by author"},
                "type": {"type": "string", "description": "Filter by message type"},
            },
            "required": ["conversation_id"],
        },
    ),
    Tool(
        name="get_pinned",
        description="Get the current pinned (source-of-truth) message for a conversation.",
        inputSchema={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Which conversation"},
            },
            "required": ["conversation_id"],
        },
    ),
    Tool(
        name="pin",
        description="Pin a specific message by ID. Auto-unpins any previously pinned message.",
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {"type": "integer", "description": "The message ID to pin"},
            },
            "required": ["message_id"],
        },
    ),
    Tool(
        name="conversations",
        description="List conversations, optionally filtered by project or search term.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Filter to one project"},
                "search": {"type": "string", "description": "Text search across conversation goals"},
            },
        },
    ),
    Tool(
        name="archive",
        description="Soft-archive a resolved conversation. Won't appear on board by default.",
        inputSchema={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Which conversation to archive"},
            },
            "required": ["conversation_id"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Project Tools
# ---------------------------------------------------------------------------

PROJECT_TOOLS = [
    Tool(
        name="create_project",
        description="Register a project for task dispatch. Configures repo, working directory, setup commands, and resource limits.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Project slug, e.g. ym-discount-engine"},
                "repo": {"type": "string", "description": "Git repo URL, e.g. git@github.com:org/repo.git"},
                "working_dir": {"type": "string", "description": "Base path on VPS for worktrees, e.g. /work/ym-discount-engine"},
                "default_branch": {"type": "string", "description": "Main branch name", "default": "main"},
                "setup_command": {"type": "string", "description": "Run after worktree creation, e.g. 'composer install && php artisan migrate --seed --env=testing'"},
                "teardown_command": {"type": "string", "description": "Run on task cleanup"},
                "test_command": {"type": "string", "description": "Hint for CC, e.g. 'php artisan test'"},
                "env_overrides": {"type": "object", "description": "Key-value env vars written to .env.testing in worktree, e.g. {\"DB_CONNECTION\": \"sqlite\"}"},
                "max_turns": {"type": "integer", "description": "Default max turns per dispatch for this project"},
                "max_wall_clock": {"type": "integer", "description": "Default max wall clock minutes per dispatch"},
                "claude_md_path": {"type": "string", "description": "Path to CLAUDE.md relative to repo root"},
            },
            "required": ["id", "repo", "working_dir"],
        },
    ),
    Tool(
        name="get_project",
        description="Get a project's configuration.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Project ID"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="list_projects",
        description="List all registered projects.",
        inputSchema={"type": "object", "properties": {}},
    ),
]

# ---------------------------------------------------------------------------
# Task Tools
# ---------------------------------------------------------------------------

TASK_TOOLS = [
    Tool(
        name="dispatch_task",
        description="Create a task and fork an autonomous CC session. Non-blocking — returns immediately with task ID and PID. CC works in an isolated git worktree.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Which project"},
                "id": {"type": "string", "description": "Task slug — becomes the branch name"},
                "goal": {"type": "string", "description": "One-liner purpose"},
                "spec": {"type": "string", "description": "Full markdown spec (becomes pinned message)"},
                "checklist": {"type": "array", "items": {"type": "string"}, "description": "Initial checklist items"},
                "phase": {"type": "string", "enum": ["analysis", "implementing"], "description": "Starting phase", "default": "analysis"},
                "max_turns": {"type": "integer", "description": "Turn limit for this dispatch (overrides project default)"},
                "max_wall_clock": {"type": "integer", "description": "Wall clock timeout in minutes (overrides project default)"},
                "escalation_criteria": {"type": "string", "description": "Markdown string appended to CC's system context for when to escalate"},
            },
            "required": ["project_id", "id", "goal"],
        },
    ),
    Tool(
        name="resume_task",
        description="Resume a paused (needs-review) task. Reuses the same session for context preservation.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task to resume"},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="retry_task",
        description="Start a fresh CC session for a task. Optionally clean the worktree (git checkout .).",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task to retry"},
                "clean": {"type": "boolean", "description": "If true, git checkout . to discard CC's changes", "default": False},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="cancel_task",
        description="Kill a running task. SIGTERM the CC process, mark as cancelled. Worktree preserved.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task to cancel"},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="close_task",
        description="Mark task as completed and optionally clean up worktree + branch.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task to close"},
                "cleanup": {"type": "boolean", "description": "Remove worktree and delete branch", "default": True},
                "force_delete_branch": {"type": "boolean", "description": "Use git branch -D instead of -d (for unmerged branches)", "default": False},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="get_task_status",
        description="Comprehensive task status — checklist progress, PID liveness, recent messages, artifacts, token usage.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task to check"},
                "include_log_tail": {"type": "boolean", "description": "Include last 30 lines of CC stdout", "default": False},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="list_tasks",
        description="List tasks, optionally filtered by project and/or status.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter to one project"},
                "status": {"type": "string", "description": "Filter by status: ready, working, needs-review, completed, failed, cancelled"},
            },
        },
    ),
    Tool(
        name="update_task_checklist",
        description="Mark a checklist item as done or not done.",
        inputSchema={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer", "description": "Checklist item ID"},
                "done": {"type": "boolean", "description": "Whether the item is complete"},
            },
            "required": ["item_id", "done"],
        },
    ),
    Tool(
        name="update_task_phase",
        description="Update a task's phase label and optional detail text. Called by CC to indicate what it's working on.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "phase": {"type": "string", "description": "Phase label: analysis, implementing, or free-text"},
                "detail": {"type": "string", "description": "Free-text detail, e.g. 'Writing BuyXGetYStrategy class'"},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="post_task_message",
        description="Post a message to a task's message thread. Used by CC for progress updates, questions, and results.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "author": {"type": "string", "description": "Who is posting"},
                "content": {"type": "string", "description": "Full markdown body"},
                "type": {"type": "string", "description": "Message type: progress, question, answer, note, result"},
                "title": {"type": "string", "description": "Optional short subject line"},
                "pinned": {"type": "boolean", "description": "Pin this message", "default": False},
            },
            "required": ["task_id", "author", "content"],
        },
    ),
    Tool(
        name="read_task_messages",
        description="Read messages from a task's thread. Supports cursor-based polling.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "after": {"type": "integer", "description": "Cursor for polling"},
                "last_n": {"type": "integer", "description": "Return only N most recent"},
                "type": {"type": "string", "description": "Filter by message type"},
            },
            "required": ["task_id"],
        },
    ),
]

TOOLS = CONVERSATION_TOOLS + PROJECT_TOOLS + TASK_TOOLS


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


async def _dispatch_tool(name: str, arguments: dict):
    # --- Conversation tools ---
    if name == "board":
        return await db.board(
            project=arguments.get("project"),
            include_archived=arguments.get("include_archived", False),
        )

    elif name == "create_conversation":
        result = await db.create_conversation(
            id=arguments["id"],
            project=arguments["project"],
            goal=arguments["goal"],
        )
        if arguments.get("content"):
            msg = await db.post_message(
                conversation_id=arguments["id"],
                author=arguments.get("author", "human"),
                content=arguments["content"],
                type=arguments.get("type"),
                title=arguments.get("title"),
            )
            result["initial_message"] = msg
        return result

    elif name == "post":
        return await db.post_message(
            conversation_id=arguments["conversation_id"],
            author=arguments["author"],
            content=arguments["content"],
            type=arguments.get("type"),
            title=arguments.get("title"),
            pinned=arguments.get("pinned", False),
        )

    elif name == "read":
        return await db.read_messages(
            conversation_id=arguments["conversation_id"],
            after=arguments.get("after"),
            last_n=arguments.get("last_n"),
            since=arguments.get("since"),
            author=arguments.get("author"),
            type=arguments.get("type"),
        )

    elif name == "get_pinned":
        result = await db.get_pinned(arguments["conversation_id"])
        return result if result else {"message": "No pinned message in this conversation"}

    elif name == "pin":
        return await db.pin_message(arguments["message_id"])

    elif name == "conversations":
        return await db.list_conversations(
            project=arguments.get("project"),
            search=arguments.get("search"),
        )

    elif name == "archive":
        return await db.archive_conversation(arguments["conversation_id"])

    # --- Project tools ---
    elif name == "create_project":
        return await db.create_project(
            id=arguments["id"],
            repo=arguments["repo"],
            working_dir=arguments["working_dir"],
            default_branch=arguments.get("default_branch", "main"),
            setup_command=arguments.get("setup_command"),
            teardown_command=arguments.get("teardown_command"),
            test_command=arguments.get("test_command"),
            env_overrides=arguments.get("env_overrides"),
            max_turns=arguments.get("max_turns"),
            max_wall_clock=arguments.get("max_wall_clock"),
            claude_md_path=arguments.get("claude_md_path"),
        )

    elif name == "get_project":
        result = await db.get_project(arguments["id"])
        return result if result else {"error": f"Project '{arguments['id']}' not found"}

    elif name == "list_projects":
        return await db.list_projects()

    # --- Task tools ---
    elif name == "dispatch_task":
        # Auto-prefix task ID with project to avoid global collisions
        project_id = arguments["project_id"]
        raw_id = arguments["id"]
        task_id = f"{project_id}/{raw_id}" if "/" not in raw_id else raw_id
        return await tasks.dispatch_task(
            project_id=project_id,
            task_id=task_id,
            goal=arguments["goal"],
            spec=arguments.get("spec"),
            checklist=arguments.get("checklist"),
            phase=arguments.get("phase", "analysis"),
            max_turns=arguments.get("max_turns"),
            max_wall_clock=arguments.get("max_wall_clock"),
            escalation_criteria=arguments.get("escalation_criteria"),
        )

    elif name == "resume_task":
        return await tasks.resume_task(arguments["task_id"])

    elif name == "retry_task":
        return await tasks.retry_task(
            task_id=arguments["task_id"],
            clean=arguments.get("clean", False),
        )

    elif name == "cancel_task":
        return await tasks.cancel_task(arguments["task_id"])

    elif name == "close_task":
        return await tasks.close_task(
            task_id=arguments["task_id"],
            cleanup=arguments.get("cleanup", True),
            force_delete_branch=arguments.get("force_delete_branch", False),
        )

    elif name == "get_task_status":
        result = await db.get_task_status(arguments["task_id"])
        # Liveness detection based on status + last_activity
        result["alive"] = result.get("status") == "working"
        if result["alive"] and result.get("last_activity"):
            from datetime import datetime, timezone
            last = datetime.fromisoformat(result["last_activity"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - last).total_seconds()
            result["stale"] = age > 900  # 15 minutes with no activity
            result["idle_minutes"] = round(age / 60, 1)
        else:
            result["stale"] = False

        # Fallback PID check for legacy/CLI tasks
        if result.get("pid"):
            result["pid_alive"] = tasks._is_pid_alive(result["pid"])

        # Optional log tail
        if arguments.get("include_log_tail") and result.get("worktree_path"):
            log_path = os.path.join(result["worktree_path"], ".switchboard", "cc-stderr.log")
            result["log_tail"] = tasks._tail_file(log_path, 30)

        return result

    elif name == "list_tasks":
        return await db.list_tasks(
            project_id=arguments.get("project_id"),
            status=arguments.get("status"),
        )

    elif name == "update_task_checklist":
        return await db.update_checklist_item(
            item_id=arguments["item_id"],
            done=arguments["done"],
        )

    elif name == "update_task_phase":
        fields = {}
        if "phase" in arguments:
            fields["phase"] = arguments["phase"]
        if "detail" in arguments:
            fields["phase"] = f"{arguments.get('phase', 'working')}: {arguments['detail']}"
        fields["last_activity"] = db.now_iso()
        return await db.update_task(arguments["task_id"], **fields)

    elif name == "post_task_message":
        return await db.post_task_message(
            task_id=arguments["task_id"],
            author=arguments["author"],
            content=arguments["content"],
            type=arguments.get("type"),
            title=arguments.get("title"),
            pinned=arguments.get("pinned", False),
        )

    elif name == "read_task_messages":
        return await db.read_task_messages(
            task_id=arguments["task_id"],
            after=arguments.get("after"),
            last_n=arguments.get("last_n"),
            type=arguments.get("type"),
        )

    else:
        raise ValueError(f"Unknown tool: {name}")


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
                message = await receive()
                if message["type"] == "lifespan.shutdown":
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

    import uvicorn
    config = uvicorn.Config(protected_app, host="0.0.0.0", port=port, log_level="info")
    srv = uvicorn.Server(config)
    await srv.serve()


if __name__ == "__main__":
    asyncio.run(main())
