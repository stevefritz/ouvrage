import asyncio
import json
import os
import re
from datetime import datetime, timezone

import uvicorn
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool, TextContent

import auth
import dashboard_api
import database as db
import notifications as notify
import tasks

PR_URL_RE = re.compile(r'https://github\.com/[^\s)]+/pull/\d+')

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
        name="update_project",
        description="Update a project's configuration. Only provided fields are changed.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Project ID"},
                "repo": {"type": "string", "description": "Git repo URL"},
                "default_branch": {"type": "string", "description": "Default branch name"},
                "working_dir": {"type": "string", "description": "Base path for worktrees"},
                "setup_command": {"type": ["string", "null"], "description": "Run after worktree creation"},
                "teardown_command": {"type": ["string", "null"], "description": "Run on cleanup"},
                "test_command": {"type": ["string", "null"], "description": "Hint for CC"},
                "env_overrides": {"type": ["object", "null"], "description": "Key-value env vars for .env.testing"},
                "max_turns": {"type": ["integer", "null"], "description": "Project-level turn limit"},
                "max_wall_clock": {"type": ["integer", "null"], "description": "Project-level wall clock limit (minutes)"},
                "claude_md_path": {"type": ["string", "null"], "description": "Path to CLAUDE.md relative to repo root"},
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
                "branch": {"type": "string", "description": "Git branch name (supports slashes like feature/foo). Defaults to the task ID slug."},
                "jira_ticket": {"type": "string", "description": "Optional Jira ticket ID or URL, e.g. 'SUZY-1324' or full URL"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags for filtering, e.g. ['bugfix', 'review']"},
                "conversation_id": {"type": "string", "description": "Optional conversation ID to link this task to a design conversation"},
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
        description="Start a fresh CC session for a task. If review feedback was posted (via post_task_message) after the last CC result, it is automatically injected as revision instructions. Workflow: post feedback with type='review', then retry. Optionally clean the worktree (git checkout .).",
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
        description="List tasks, optionally filtered by project, status, and/or tag.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter to one project"},
                "status": {"type": "string", "description": "Filter by status: ready, working, needs-review, completed, failed, cancelled"},
                "tag": {"type": "string", "description": "Filter by tag"},
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
    Tool(
        name="get_session_log",
        description="Get the session log (JSONL) for a task — shows CC's tool calls, text output, and results.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "tail": {"type": "integer", "description": "Return only the last N entries. Default 50.", "default": 50},
                "types": {"type": "string", "description": "Comma-separated type filter, e.g. 'text,tool,result'. Omit for all types."},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="get_dispatch_log",
        description="Get the dispatch log for a task — shows dispatch/completion metadata, cost, tokens, timing.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "tail": {"type": "integer", "description": "Return only the last N lines. Default 20.", "default": 20},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="add_checklist_item",
        description="Add a new deliverable to a task's checklist. Used by CC to add missing items discovered during grounding.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "item": {"type": "string", "description": "Checklist item text"},
            },
            "required": ["task_id", "item"],
        },
    ),
    Tool(
        name="remove_checklist_item",
        description="Remove an irrelevant checklist item. Used by CC when a deliverable doesn't apply after reading the code.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID (for context, item_id is sufficient for deletion)"},
                "item_id": {"type": "integer", "description": "Checklist item ID to remove"},
            },
            "required": ["item_id"],
        },
    ),
    Tool(
        name="update_checklist_item",
        description="Update the text of a checklist item. Used by CC to correct inaccurate deliverables.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID (for context)"},
                "item_id": {"type": "integer", "description": "Checklist item ID to update"},
                "text": {"type": "string", "description": "New text for the checklist item"},
            },
            "required": ["item_id", "text"],
        },
    ),
    Tool(
        name="search_task_messages",
        description="Full-text search across all task message content. Returns matching messages with task_id, author, type, content snippet.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
                "project_id": {"type": "string", "description": "Optional: filter to one project"},
                "limit": {"type": "integer", "description": "Max results to return (default 20)", "default": 20},
            },
            "required": ["query"],
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


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------


async def _handle_board(arguments):
    return await db.board(
        project=arguments.get("project"),
        include_archived=arguments.get("include_archived", False),
    )


async def _handle_create_conversation(arguments):
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


async def _handle_post(arguments):
    return await db.post_message(
        conversation_id=arguments["conversation_id"],
        author=arguments["author"],
        content=arguments["content"],
        type=arguments.get("type"),
        title=arguments.get("title"),
        pinned=arguments.get("pinned", False),
    )


async def _handle_read(arguments):
    return await db.read_messages(
        conversation_id=arguments["conversation_id"],
        after=arguments.get("after"),
        last_n=arguments.get("last_n"),
        since=arguments.get("since"),
        author=arguments.get("author"),
        type=arguments.get("type"),
    )


async def _handle_get_pinned(arguments):
    result = await db.get_pinned(arguments["conversation_id"])
    return result if result else {"message": "No pinned message in this conversation"}


async def _handle_pin(arguments):
    return await db.pin_message(arguments["message_id"])


async def _handle_update_project(arguments):
    project_id = arguments.pop("id")
    if not arguments:
        return {"error": "No fields to update"}
    return await db.update_project(project_id, **arguments)

async def _handle_conversations(arguments):
    return await db.list_conversations(
        project=arguments.get("project"),
        search=arguments.get("search"),
    )


async def _handle_archive(arguments):
    return await db.archive_conversation(arguments["conversation_id"])


async def _handle_create_project(arguments):
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

async def _handle_get_project(arguments):
    result = await db.get_project(arguments["id"])
    return result if result else {"error": f"Project '{arguments['id']}' not found"}


async def _handle_list_projects(arguments):
    return await db.list_projects()


async def _handle_dispatch_task(arguments):
    # Auto-prefix task ID with project to avoid global collisions
    project_id = arguments["project_id"]
    raw_id = arguments["id"]
    task_id = f"{project_id}/{raw_id}" if "/" not in raw_id else raw_id
    result = await tasks.dispatch_task(
        project_id=project_id,
        task_id=task_id,
        goal=arguments["goal"],
        spec=arguments.get("spec"),
        checklist=arguments.get("checklist"),
        phase=arguments.get("phase", "analysis"),
        max_turns=arguments.get("max_turns"),
        max_wall_clock=arguments.get("max_wall_clock"),
        escalation_criteria=arguments.get("escalation_criteria"),
        branch=arguments.get("branch"),
        jira_ticket=arguments.get("jira_ticket"),
        conversation_id=arguments.get("conversation_id"),
    )
    # Set tags if provided
    tags = arguments.get("tags")
    if tags:
        await db.set_task_tags(task_id, tags)
        result["tags"] = tags
    return result

async def _handle_resume_task(arguments):
    return await tasks.resume_task(arguments["task_id"])


async def _handle_retry_task(arguments):
    return await tasks.retry_task(
        task_id=arguments["task_id"],
        clean=arguments.get("clean", False),
    )


async def _handle_cancel_task(arguments):
    return await tasks.cancel_task(arguments["task_id"])


async def _handle_close_task(arguments):
    return await tasks.close_task(
        task_id=arguments["task_id"],
        cleanup=arguments.get("cleanup", True),
        force_delete_branch=arguments.get("force_delete_branch", False),
    )


async def _handle_get_task_status(arguments):
    result = await db.get_task_status(arguments["task_id"])
    # Liveness detection based on status + last_activity
    result["alive"] = result.get("status") == "working"
    if result["alive"] and result.get("last_activity"):
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


async def _handle_list_tasks(arguments):
    return await db.list_tasks(
        project_id=arguments.get("project_id"),
        status=arguments.get("status"),
        tag=arguments.get("tag"),
    )


async def _handle_update_task_checklist(arguments):
    result = await db.update_checklist_item(
        item_id=arguments["item_id"],
        done=arguments["done"],
    )
    # Notify on checklist progress
    if arguments.get("done") and result.get("task_id"):
        checklist = await db.get_checklist(result["task_id"])
        done_count = sum(1 for c in checklist if c.get("done"))
        await notify.checklist_progress(
            task_id=result["task_id"],
            item_text=result.get("item", ""),
            done=done_count,
            total=len(checklist),
        )
    return result


async def _handle_update_task_phase(arguments):
    fields = {}
    if "detail" in arguments:
        fields["phase"] = f"{arguments.get('phase', 'working')}: {arguments['detail']}"
    elif "phase" in arguments:
        fields["phase"] = arguments["phase"]
    fields["last_activity"] = db.now_iso()
    result = await db.update_task(arguments["task_id"], **fields)
    await notify.task_phase_changed(
        task_id=arguments["task_id"],
        phase=fields.get("phase", "working"),
    )
    return result


async def _handle_post_task_message(arguments):
    result = await db.post_task_message(
        task_id=arguments["task_id"],
        author=arguments["author"],
        content=arguments["content"],
        type=arguments.get("type"),
        title=arguments.get("title"),
        pinned=arguments.get("pinned", False),
    )
    # Notify Slack on progress, result, and question messages
    msg_type = arguments.get("type", "")
    if msg_type == "question":
        await notify.task_question(
            task_id=arguments["task_id"],
            question=arguments["content"],
        )
    elif msg_type in ("progress", "result"):
        await notify.task_progress(
            task_id=arguments["task_id"],
            title=arguments.get("title"),
            content=arguments["content"],
            msg_type=msg_type,
        )
    # Auto-extract PR URLs from result/progress messages
    if msg_type in ("result", "progress"):
        urls = PR_URL_RE.findall(arguments.get("content", ""))
        for url in urls:
            await db.add_artifact(arguments["task_id"], type="pr_url", ref=url)
    return result


async def _handle_read_task_messages(arguments):
    return await db.read_task_messages(
        task_id=arguments["task_id"],
        after=arguments.get("after"),
        last_n=arguments.get("last_n"),
        type=arguments.get("type"),
    )


async def _handle_get_session_log(arguments):
    task = await db.get_task(arguments["task_id"])
    if not task:
        return {"error": f"Task '{arguments['task_id']}' not found"}
    worktree_path = task.get("worktree_path")
    if not worktree_path:
        return {"error": "Task has no worktree path (not dispatched or already cleaned up)"}

    log_path = os.path.join(worktree_path, ".switchboard", "session.jsonl")
    if not os.path.isfile(log_path):
        return {"entries": [], "message": "No session log file found"}

    tail = arguments.get("tail", 50)
    type_filter = None
    if arguments.get("types"):
        type_filter = {t.strip() for t in arguments["types"].split(",")}

    entries = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if type_filter and entry.get("type") not in type_filter:
                    continue
                entries.append(entry)
    except Exception as e:
        return {"error": f"Failed to read session log: {e}"}

    # Apply tail
    entries = entries[-tail:]

    # Truncate large content fields to keep response size reasonable
    for entry in entries:
        if isinstance(entry.get("content"), list):
            for block in entry["content"]:
                for key in ("text", "preview", "input"):
                    if isinstance(block.get(key), str) and len(block[key]) > 500:
                        block[key] = block[key][:500] + "... [truncated]"
        if isinstance(entry.get("result"), str) and len(entry["result"]) > 500:
            entry["result"] = entry["result"][:500] + "... [truncated]"

    return {"entries": entries, "count": len(entries)}


async def _handle_get_dispatch_log(arguments):
    task = await db.get_task(arguments["task_id"])
    if not task:
        return {"error": f"Task '{arguments['task_id']}' not found"}
    worktree_path = task.get("worktree_path")
    if not worktree_path:
        return {"error": "Task has no worktree path (not dispatched or already cleaned up)"}

    log_path = os.path.join(worktree_path, ".switchboard", "dispatch.log")
    if not os.path.isfile(log_path):
        return {"text": "", "message": "No dispatch log file found"}

    tail = arguments.get("tail", 20)
    try:
        with open(log_path) as f:
            lines = f.readlines()
        text = "".join(lines[-tail:])
    except Exception as e:
        return {"error": f"Failed to read dispatch log: {e}"}

    return {"text": text}


async def _handle_add_checklist_item(arguments):
    return await db.add_checklist_item(
        task_id=arguments["task_id"],
        item=arguments["item"],
    )


async def _handle_remove_checklist_item(arguments):
    return await db.remove_checklist_item(item_id=arguments["item_id"])


async def _handle_update_checklist_item_text(arguments):
    return await db.update_checklist_item_text(
        item_id=arguments["item_id"],
        text=arguments["text"],
    )


async def _handle_search_task_messages(arguments):
    return await db.search_task_messages(
        query=arguments["query"],
        project_id=arguments.get("project_id"),
        limit=arguments.get("limit", 20),
    )


TOOL_HANDLERS = {
    # Conversation tools
    "board": _handle_board,
    "create_conversation": _handle_create_conversation,
    "post": _handle_post,
    "read": _handle_read,
    "get_pinned": _handle_get_pinned,
    "pin": _handle_pin,
    "conversations": _handle_conversations,
    "archive": _handle_archive,
    # Project tools
    "create_project": _handle_create_project,
    "get_project": _handle_get_project,
    "update_project": _handle_update_project,
    "list_projects": _handle_list_projects,
    # Task tools
    "dispatch_task": _handle_dispatch_task,
    "resume_task": _handle_resume_task,
    "retry_task": _handle_retry_task,
    "cancel_task": _handle_cancel_task,
    "close_task": _handle_close_task,
    "get_task_status": _handle_get_task_status,
    "list_tasks": _handle_list_tasks,
    "update_task_checklist": _handle_update_task_checklist,
    "update_task_phase": _handle_update_task_phase,
    "post_task_message": _handle_post_task_message,
    "read_task_messages": _handle_read_task_messages,
    "get_session_log": _handle_get_session_log,
    "get_dispatch_log": _handle_get_dispatch_log,
    "add_checklist_item": _handle_add_checklist_item,
    "remove_checklist_item": _handle_remove_checklist_item,
    "update_checklist_item": _handle_update_checklist_item_text,
    "search_task_messages": _handle_search_task_messages,
}


async def _dispatch_tool(name: str, arguments: dict):
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        raise ValueError(f"Unknown tool: {name}")
    return await handler(arguments)


_DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
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
        elif path.startswith("/dashboard/api/"):
            await dashboard_api.handle_request(scope, receive, send)
        elif path.startswith("/dashboard"):
            await _serve_dashboard(scope, send)
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


if __name__ == "__main__":
    asyncio.run(main())
