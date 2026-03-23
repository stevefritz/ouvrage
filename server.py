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
5. If you need the full tool reference, call get_guide

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
                "claude_chat_url": {"type": "string", "description": "Optional URL linking to the claude.ai chat for this conversation"},
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
        description=(
            "Register a git repo as a Switchboard project. Each project gets its own working directory "
            "where git worktrees are created for tasks. Only 'id' and 'repo' are required — everything "
            "else has sensible defaults. The repo must exist on GitHub and have at least one commit."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Short unique slug for this project, used in task IDs and URLs. Example: 'my-app', 'api-backend'",
                },
                "repo": {
                    "type": "string",
                    "description": "SSH git clone URL. Example: 'git@github.com:yourorg/your-repo.git'. The server clones this repo and creates worktrees from it.",
                },
                "folder_name": {
                    "type": "string",
                    "description": "Override the folder name under the worktree base directory. Defaults to the repo name (e.g. 'your-repo'). You do NOT set a full path — just the folder name.",
                },
                "default_branch": {
                    "type": "string",
                    "description": "The main/trunk branch that tasks branch from and merge into. Default: 'main'",
                    "default": "main",
                },
                "setup_command": {
                    "type": "string",
                    "description": "Shell command run in each new worktree after creation. Use for dependency install, DB setup, etc. Example: 'npm install' or 'composer install && php artisan migrate --seed'",
                },
                "teardown_command": {
                    "type": "string",
                    "description": "Shell command run when a worktree is cleaned up. Rarely needed.",
                },
                "test_command": {
                    "type": "string",
                    "description": "The command used by the auto-test gate after task completion. Example: 'npm test', 'pytest', 'php artisan test'. If set, tasks with auto_test=true will run this automatically.",
                },
                "env_overrides": {
                    "type": "object",
                    "description": "Key-value pairs appended to .env.testing in each worktree. Example: {\"DB_CONNECTION\": \"sqlite\", \"APP_ENV\": \"testing\"}",
                },
                "max_turns": {
                    "type": "integer",
                    "description": "Default limit on Claude Code conversation turns per task dispatch. Higher = more autonomy, more cost. Default: 200",
                },
                "max_wall_clock": {
                    "type": "integer",
                    "description": "Default time limit in minutes per task dispatch. Task is paused when exceeded. Default: 30",
                },
                "claude_md_path": {
                    "type": "string",
                    "description": "Path to a CLAUDE.md file relative to repo root. Loaded as context for CC workers. Example: 'CLAUDE.md' or 'docs/CLAUDE.md'",
                },
                "model": {
                    "type": "string",
                    "enum": ["sonnet", "opus"],
                    "description": "Default Claude model for tasks. 'sonnet' is faster/cheaper, 'opus' is more capable. Default: sonnet",
                },
                "state_definitions": {
                    "type": "object",
                    "description": "Advanced: custom status colors/labels for dashboard rendering. Most users don't need this.",
                },
            },
            "required": ["id", "repo"],
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
                "setup_command": {"type": ["string", "null"], "description": "Run after worktree creation"},
                "teardown_command": {"type": ["string", "null"], "description": "Run on cleanup"},
                "test_command": {"type": ["string", "null"], "description": "Hint for CC"},
                "env_overrides": {"type": ["object", "null"], "description": "Key-value env vars for .env.testing"},
                "max_turns": {"type": ["integer", "null"], "description": "Project-level turn limit"},
                "max_wall_clock": {"type": ["integer", "null"], "description": "Project-level wall clock limit (minutes)"},
                "claude_md_path": {"type": ["string", "null"], "description": "Path to CLAUDE.md relative to repo root"},
                "state_definitions": {"type": ["object", "null"], "description": "Custom state definitions for dashboard rendering"},
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
                "model": {"type": "string", "enum": ["sonnet", "opus"], "description": "Claude model for this task (overrides project default). Default: sonnet"},
                "auto_test": {"type": "boolean", "description": "Run test_command after completion as a gate. Default: true", "default": True},
                "auto_review": {"type": "boolean", "description": "Dispatch a self-review session after tests pass. Default: true", "default": True},
                "review_model": {"type": "string", "enum": ["sonnet", "opus"], "description": "Model for self-review task. Default: opus"},
                "auto_pr": {"type": "boolean", "description": "Auto-create PR when chain tail passes all gates. Default: false", "default": False},
                "auto_merge": {"type": "boolean", "description": "Auto-merge task branch into target on gate pass. Mutually exclusive with auto_pr. Default: false", "default": False},
                "auto_release_worktree": {"type": "boolean", "description": "Auto-detach worktree after gate pass. Default: true", "default": True},
                "base_branch": {"type": "string", "description": "Override merge target branch (defaults to project default_branch)"},
                "depends_on": {"type": "string", "description": "Task ID this depends on. Won't dispatch until parent gate-passes."},
                "component_id": {"type": "string", "description": "Optional component ID. Task inherits component config."},
                "claude_chat_url": {"type": "string", "description": "Optional URL linking to the claude.ai chat for this task"},
                "held": {"type": "boolean", "description": "Create task but don't dispatch — requires manual approval first. Use for chain checkpoints. Default: false", "default": False},
            },
            "required": ["project_id", "id", "goal"],
        },
    ),
    Tool(
        name="release_worktree",
        description="Detach a task's worktree without closing the task. Branch stays on origin. Frees the concurrency slot for queued tasks.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task whose worktree to release"},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="resume_task",
        description="Resume a paused or completed task. Reuses the same session for context preservation. Works for needs-review, turns-exhausted, or completed tasks.",
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
        description="List tasks, optionally filtered by project, status, tag, and/or component.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter to one project"},
                "status": {"type": "string", "description": "Filter by status: ready, working, needs-review, completed, failed, cancelled"},
                "tag": {"type": "string", "description": "Filter by tag"},
                "component_id": {"type": "string", "description": "Filter to one component"},
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
        description="Get the session log (JSONL) for a task — shows CC's tool calls, text output, and results. Pass attempt to read a historical archive.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "tail": {"type": "integer", "description": "Return only the last N entries. Default 50.", "default": 50},
                "types": {"type": "string", "description": "Comma-separated type filter, e.g. 'text,tool,result'. Omit for all types."},
                "attempt": {"type": "integer", "description": "Attempt number to read from archive. Omit for current/latest."},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="get_dispatch_log",
        description="Get the dispatch log for a task — shows dispatch/completion metadata, cost, tokens, timing. Pass attempt to read a historical archive.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "tail": {"type": "integer", "description": "Return only the last N lines. Default 20.", "default": 20},
                "attempt": {"type": "integer", "description": "Attempt number to read from archive. Omit for current/latest."},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="list_attempts",
        description="List all archived attempts for a task — shows attempt number, reason (retry/close/detach/completion), cost, tokens, session_id, timestamp.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
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
        name="get_pipeline",
        description="Get the full task dependency chain for any task in the chain. Returns ordered list of tasks from root to tail.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Any task ID in the chain"},
            },
            "required": ["task_id"],
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
    Tool(
        name="update_task",
        description="Update task metadata post-dispatch. Use to assign components, correct branching info, toggle gates, or update any task field. Validates component_id if provided.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to update"},
                "component_id": {"type": ["string", "null"], "description": "Assign/reassign to component. Validated to exist."},
                "base_branch": {"type": ["string", "null"], "description": "Correct base branch"},
                "branch_target": {"type": ["string", "null"], "description": "Branch target override"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Replace all tags"},
                "auto_test": {"type": ["boolean", "null"], "description": "Toggle auto-test gate"},
                "auto_review": {"type": ["boolean", "null"], "description": "Toggle auto-review gate"},
                "auto_merge": {"type": ["boolean", "null"], "description": "Toggle auto-merge"},
                "auto_pr": {"type": ["boolean", "null"], "description": "Toggle auto-PR"},
                "max_test_retries": {"type": ["integer", "null"], "description": "Max test retry attempts"},
                "max_review_retries": {"type": ["integer", "null"], "description": "Max review retry attempts"},
                "model": {"type": ["string", "null"], "enum": ["sonnet", "opus", None], "description": "Override model"},
                "jira_ticket": {"type": ["string", "null"], "description": "Jira ticket ID or URL"},
                "conversation_id": {"type": ["string", "null"], "description": "Link to design conversation"},
                "claude_chat_url": {"type": ["string", "null"], "description": "Claude.ai chat URL"},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="bulk_update_tasks",
        description="Apply the same field updates to multiple tasks in one call. Use case: assign all chatbot-* tasks to a component at once. Returns count of updated tasks.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_ids": {"type": "array", "items": {"type": "string"}, "description": "List of task IDs to update"},
                "component_id": {"type": ["string", "null"], "description": "Assign/reassign to component"},
                "base_branch": {"type": ["string", "null"], "description": "Base branch override"},
                "branch_target": {"type": ["string", "null"], "description": "Branch target override"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Replace all tags on each task"},
                "auto_test": {"type": ["boolean", "null"], "description": "Toggle auto-test gate"},
                "auto_review": {"type": ["boolean", "null"], "description": "Toggle auto-review gate"},
                "auto_merge": {"type": ["boolean", "null"], "description": "Toggle auto-merge"},
                "auto_pr": {"type": ["boolean", "null"], "description": "Toggle auto-PR"},
                "max_test_retries": {"type": ["integer", "null"], "description": "Max test retry attempts"},
                "max_review_retries": {"type": ["integer", "null"], "description": "Max review retry attempts"},
                "model": {"type": ["string", "null"], "enum": ["sonnet", "opus", None], "description": "Override model"},
                "jira_ticket": {"type": ["string", "null"], "description": "Jira ticket ID or URL"},
                "conversation_id": {"type": ["string", "null"], "description": "Link to design conversation"},
                "claude_chat_url": {"type": ["string", "null"], "description": "Claude.ai chat URL"},
            },
            "required": ["task_ids"],
        },
    ),
    Tool(
        name="move_task",
        description="Reassign a task to a different component. Validates the target component exists and belongs to the same project. Sugar for update_task with component_id.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task to reassign"},
                "component_id": {"type": "string", "description": "Target component ID"},
            },
            "required": ["task_id", "component_id"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Component Tools
# ---------------------------------------------------------------------------

COMPONENT_TOOLS = [
    Tool(
        name="create_component",
        description="Create a new component (feature/epic) within a project. Components group tasks and provide config inheritance.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project this component belongs to"},
                "id": {"type": "string", "description": "Component slug, e.g. chatbot-discovery"},
                "name": {"type": "string", "description": "Human-readable name"},
                "description": {"type": "string", "description": "What this component is about"},
                "phase": {"type": "string", "description": "Lifecycle phase: planning, building, testing, polish, review, deployed, archived", "default": "planning"},
                "base_branch": {"type": "string", "description": "Git branch for tasks in this component"},
                "model": {"type": "string", "enum": ["sonnet", "opus"], "description": "Default model for tasks"},
                "setup_command": {"type": "string", "description": "Setup command override"},
                "test_command": {"type": "string", "description": "Test command override"},
                "auto_test": {"type": "boolean", "description": "Auto-run tests after task completion"},
                "auto_review": {"type": "boolean", "description": "Auto-dispatch review after tests pass"},
                "review_model": {"type": "string", "enum": ["sonnet", "opus"], "description": "Model for reviews"},
                "max_test_retries": {"type": "integer", "description": "Max test retry attempts"},
                "max_review_retries": {"type": "integer", "description": "Max review retry attempts"},
                "auto_pr": {"type": "boolean", "description": "Auto-create PR when gates pass"},
                "auto_merge": {"type": "boolean", "description": "Auto-merge PR after approval"},
                "max_turns": {"type": "integer", "description": "Default turn limit for tasks"},
                "max_wall_clock": {"type": "integer", "description": "Default wall clock limit (minutes)"},
                "env_overrides": {"type": "object", "description": "Environment variable overrides (shallow-merged with project)"},
                "secrets": {"type": "object", "description": "Secret overrides (shallow-merged with project)"},
            },
            "required": ["project_id", "id", "name"],
        },
    ),
    Tool(
        name="update_component",
        description="Update a component's fields. Only provided fields are changed.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Component ID"},
                "name": {"type": "string", "description": "Human-readable name"},
                "description": {"type": ["string", "null"], "description": "Description"},
                "phase": {"type": "string", "description": "Lifecycle phase"},
                "base_branch": {"type": ["string", "null"], "description": "Git branch override"},
                "model": {"type": ["string", "null"], "description": "Model override"},
                "setup_command": {"type": ["string", "null"], "description": "Setup command override"},
                "test_command": {"type": ["string", "null"], "description": "Test command override"},
                "auto_test": {"type": ["boolean", "null"], "description": "Auto-test override"},
                "auto_review": {"type": ["boolean", "null"], "description": "Auto-review override"},
                "review_model": {"type": ["string", "null"], "description": "Review model override"},
                "max_test_retries": {"type": ["integer", "null"], "description": "Max test retries"},
                "max_review_retries": {"type": ["integer", "null"], "description": "Max review retries"},
                "auto_pr": {"type": ["boolean", "null"], "description": "Auto-PR override"},
                "auto_merge": {"type": ["boolean", "null"], "description": "Auto-merge override"},
                "max_turns": {"type": ["integer", "null"], "description": "Turn limit override"},
                "max_wall_clock": {"type": ["integer", "null"], "description": "Wall clock limit override"},
                "env_overrides": {"type": ["object", "null"], "description": "Env overrides"},
                "secrets": {"type": ["object", "null"], "description": "Secret overrides"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="get_component",
        description="Get a component with config, linked conversations, and task summary.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Component ID"},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="list_components",
        description="List components, optionally filtered by project.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter to one project"},
            },
        },
    ),
    Tool(
        name="link_conversation",
        description="Link a conversation to a component for context tracking.",
        inputSchema={
            "type": "object",
            "properties": {
                "component_id": {"type": "string", "description": "Component ID"},
                "conversation_id": {"type": "string", "description": "Conversation ID to link"},
            },
            "required": ["component_id", "conversation_id"],
        },
    ),
    Tool(
        name="unlink_conversation",
        description="Remove a conversation link from a component.",
        inputSchema={
            "type": "object",
            "properties": {
                "component_id": {"type": "string", "description": "Component ID"},
                "conversation_id": {"type": "string", "description": "Conversation ID to unlink"},
            },
            "required": ["component_id", "conversation_id"],
        },
    ),
    Tool(
        name="search_component",
        description="Search across all content linked to a component: messages from linked conversations and task messages.",
        inputSchema={
            "type": "object",
            "properties": {
                "component_id": {"type": "string", "description": "Component ID to search within"},
                "query": {"type": "string", "description": "Search query (substring match)"},
                "include_graphiti": {"type": "boolean", "description": "Also search Graphiti if configured on the project's connectors", "default": False},
                "limit": {"type": "integer", "description": "Max results per source (default 20)", "default": 20},
            },
            "required": ["component_id", "query"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Punchlist Tools
# ---------------------------------------------------------------------------

PUNCHLIST_TOOLS = [
    Tool(
        name="add_punchlist_item",
        description="Add a punchlist item to a component. Punchlist items are small tasks or TODOs tracked at the component level.",
        inputSchema={
            "type": "object",
            "properties": {
                "component_id": {"type": "string", "description": "Component ID"},
                "item": {"type": "string", "description": "Description of the punchlist item"},
                "author": {"type": "string", "description": "Who is adding this item (e.g. task_id or username)"},
            },
            "required": ["component_id", "item"],
        },
    ),
    Tool(
        name="list_punchlist",
        description="List punchlist items for a component. By default excludes 'done' items.",
        inputSchema={
            "type": "object",
            "properties": {
                "component_id": {"type": "string", "description": "Component ID"},
                "include_done": {"type": "boolean", "description": "Include completed items", "default": False},
                "claimed_by": {"type": "string", "description": "Filter to items claimed by a specific task_id"},
            },
            "required": ["component_id"],
        },
    ),
    Tool(
        name="claim_punchlist_item",
        description="Claim a punchlist item for a task. Sets status to 'claimed' and records which task is working on it.",
        inputSchema={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer", "description": "Punchlist item ID"},
                "task_id": {"type": "string", "description": "Task ID claiming this item"},
            },
            "required": ["item_id", "task_id"],
        },
    ),
    Tool(
        name="resolve_punchlist_item",
        description="Mark a punchlist item as done. Typically called when a task that claimed the item completes successfully.",
        inputSchema={
            "type": "object",
            "properties": {
                "item_id": {"type": "integer", "description": "Punchlist item ID"},
                "task_id": {"type": "string", "description": "Task ID that resolved this item"},
            },
            "required": ["item_id", "task_id"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Ops Tools
# ---------------------------------------------------------------------------

OPS_TOOLS = [
    Tool(
        name="get_context",
        description=(
            "START HERE — call this first in every conversation. Returns a compact snapshot of "
            "current system state: active projects, running/blocked tasks, recent events, and "
            "pinned conversations. Enough to orient without reading the full guide."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_guide",
        description="Full tool reference and workflow guide. Call this when get_context isn't enough — e.g. first time using Switchboard, or need to understand a specific workflow pattern.",
        inputSchema={"type": "object", "properties": {}},
    ),
]

CONTROL_TOOLS = [
    Tool(
        name="pause_component",
        description="Pause a component — no new tasks will be dispatched. Running tasks finish naturally.",
        inputSchema={"type": "object", "properties": {"component_id": {"type": "string"}}, "required": ["component_id"]},
    ),
    Tool(
        name="resume_component",
        description="Resume a paused component — tasks can be dispatched again.",
        inputSchema={"type": "object", "properties": {"component_id": {"type": "string"}}, "required": ["component_id"]},
    ),
    Tool(
        name="stop_component",
        description="Stop a component — pause it AND cancel all running tasks immediately.",
        inputSchema={"type": "object", "properties": {"component_id": {"type": "string"}}, "required": ["component_id"]},
    ),
    Tool(
        name="pause_project",
        description="Pause a project — no new tasks will be dispatched. Running tasks finish naturally.",
        inputSchema={"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"]},
    ),
    Tool(
        name="resume_project",
        description="Resume a paused project — tasks can be dispatched again.",
        inputSchema={"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"]},
    ),
    Tool(
        name="stop_project",
        description="Stop a project — pause it AND cancel all running tasks immediately.",
        inputSchema={"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"]},
    ),
]

TOOLS = CONVERSATION_TOOLS + PROJECT_TOOLS + TASK_TOOLS + COMPONENT_TOOLS + PUNCHLIST_TOOLS + OPS_TOOLS + CONTROL_TOOLS


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
        claude_chat_url=arguments.get("claude_chat_url"),
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


WORKTREE_BASE = os.environ.get("WORKTREE_BASE", "/work")


def _resolve_working_dir(repo: str, folder_name: str | None = None) -> str:
    """Derive working_dir from repo URL and optional folder name override."""
    if folder_name:
        name = folder_name
    else:
        # Extract repo name from URL: git@github.com:org/repo.git → repo
        name = repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        # Also handle ssh colon syntax: git@github.com:org/repo.git
        if ":" in name:
            name = name.rsplit(":", 1)[-1].removesuffix(".git")
    # Sanitize — no path traversal
    name = name.replace("/", "").replace("..", "").replace("\\", "")
    if not name:
        raise ValueError("Could not derive folder name from repo URL")
    return os.path.join(WORKTREE_BASE, name)


async def _handle_create_project(arguments):
    working_dir = arguments.get("working_dir") or _resolve_working_dir(
        arguments["repo"], arguments.get("folder_name")
    )
    # Enforce worktree base — no escaping
    resolved = os.path.realpath(working_dir)
    base = os.path.realpath(WORKTREE_BASE)
    if not resolved.startswith(base + "/") and resolved != base:
        raise ValueError(f"working_dir must be under {WORKTREE_BASE}, got: {working_dir}")

    return await db.create_project(
        id=arguments["id"],
        repo=arguments["repo"],
        working_dir=resolved,
        default_branch=arguments.get("default_branch", "main"),
        setup_command=arguments.get("setup_command"),
        teardown_command=arguments.get("teardown_command"),
        test_command=arguments.get("test_command"),
        env_overrides=arguments.get("env_overrides"),
        max_turns=arguments.get("max_turns"),
        max_wall_clock=arguments.get("max_wall_clock"),
        claude_md_path=arguments.get("claude_md_path"),
        model=arguments.get("model"),
        state_definitions=arguments.get("state_definitions"),
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
        model=arguments.get("model"),
        auto_test=arguments.get("auto_test", True),
        auto_review=arguments.get("auto_review", True),
        review_model=arguments.get("review_model"),
        auto_pr=arguments.get("auto_pr", False),
        auto_merge=arguments.get("auto_merge", False),
        auto_release_worktree=arguments.get("auto_release_worktree", True),
        base_branch=arguments.get("base_branch"),
        component_id=arguments.get("component_id"),
        claude_chat_url=arguments.get("claude_chat_url"),
        depends_on=(f"{project_id}/{arguments['depends_on']}"
                    if arguments.get("depends_on") and "/" not in arguments["depends_on"]
                    else arguments.get("depends_on")),
        held=arguments.get("held", False),
    )
    # Set tags if provided
    tags = arguments.get("tags")
    if tags:
        await db.set_task_tags(task_id, tags)
        result["tags"] = tags
    return result

async def _handle_release_worktree(arguments):
    return await tasks.release_worktree(arguments["task_id"])

async def _handle_resume_task(arguments):
    return await tasks.resume_task(arguments["task_id"])

async def _handle_approve_task(arguments):
    return await tasks.approve_task(arguments["task_id"])


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
    # Add resolved config
    try:
        result["resolved_config"] = await db.resolve_config(arguments["task_id"])
    except Exception:
        pass
    # Liveness detection based on status + last_activity
    result["alive"] = result.get("status") == "working"
    stale_seconds = 0
    if result["alive"] and result.get("last_activity"):
        last = datetime.fromisoformat(result["last_activity"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - last).total_seconds()
        stale_seconds = round(age)
        result["stale"] = age > 900  # 15 minutes with no activity
        result["idle_minutes"] = round(age / 60, 1)
    else:
        result["stale"] = False
    result["stale_seconds"] = stale_seconds

    # Fallback PID check for legacy/CLI tasks
    if result.get("pid"):
        result["pid_alive"] = tasks._is_pid_alive(result["pid"])

    # State definition for dashboard rendering
    project = await db.get_project(result.get("project_id", ""))
    result["state_definition"] = db.get_state_definition(result.get("status", ""), project)

    # Optional log tail
    if arguments.get("include_log_tail") and result.get("worktree_path"):
        log_path = os.path.join(result["worktree_path"], ".switchboard", "cc-stderr.log")
        result["log_tail"] = tasks._tail_file(log_path, 30)

    return result


async def _handle_list_tasks(arguments):
    task_list = await db.list_tasks(
        project_id=arguments.get("project_id"),
        status=arguments.get("status"),
        tag=arguments.get("tag"),
        component_id=arguments.get("component_id"),
    )
    # Cache project lookups for state definitions
    project_cache: dict[str, dict | None] = {}
    for task in task_list:
        pid = task.get("project_id", "")
        if pid not in project_cache:
            project_cache[pid] = await db.get_project(pid)
        task["state_definition"] = db.get_state_definition(task.get("status", ""), project_cache[pid])
    return task_list


_UPDATE_TASK_FIELDS = {
    "component_id", "base_branch", "branch_target", "tags",
    "auto_test", "auto_review", "auto_merge", "auto_pr",
    "max_test_retries", "max_review_retries",
    "model", "jira_ticket", "conversation_id", "claude_chat_url",
}


async def _handle_update_task(arguments):
    task_id = arguments["task_id"]
    fields = {k: v for k, v in arguments.items() if k in _UPDATE_TASK_FIELDS}
    return await db.update_task(task_id, **fields)


async def _handle_bulk_update_tasks(arguments):
    task_ids = arguments["task_ids"]
    fields = {k: v for k, v in arguments.items() if k in _UPDATE_TASK_FIELDS}
    count = await db.bulk_update_tasks(task_ids, **fields)
    return {"updated": count, "requested": len(task_ids)}


async def _handle_move_task(arguments):
    return await db.move_task(arguments["task_id"], arguments["component_id"])


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


def _resolve_log_dir(task: dict, project: dict | None, attempt: int | None) -> tuple[str | None, str | None]:
    """Return (log_dir_path, source_label) for reading logs.

    Priority:
    1. If attempt specified → read from archive
    2. If worktree exists → read from live worktree
    3. Fallback → read from highest-numbered archive
    Returns (path_or_None, label).
    """
    if attempt is not None:
        if not project:
            return None, "archive"
        archive = tasks._find_archive_path(project, task["id"], attempt)
        return str(archive) if archive else None, f"archive attempt-{attempt}"

    worktree = task.get("worktree_path")
    if worktree and os.path.isdir(os.path.join(worktree, ".switchboard")):
        return os.path.join(worktree, ".switchboard"), "live"

    # Fallback to highest archive
    if project:
        archive = tasks._find_archive_path(project, task["id"], None)
        if archive:
            return str(archive), "archive (latest)"

    return None, None


async def _handle_get_session_log(arguments):
    task_id = arguments["task_id"]
    task = await db.get_task(task_id)
    if not task:
        return {"error": f"Task '{task_id}' not found"}

    attempt = arguments.get("attempt")
    project = await db.get_project(task["project_id"]) if task.get("project_id") else None
    log_dir, source = _resolve_log_dir(task, project, attempt)

    if not log_dir:
        return {"error": "No log data found (no live worktree and no archived attempts)"}

    log_path = os.path.join(log_dir, "session.jsonl")
    if not os.path.isfile(log_path):
        return {"entries": [], "message": "No session log file found", "source": source}

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

    return {"entries": entries, "count": len(entries), "source": source}


async def _handle_get_dispatch_log(arguments):
    task_id = arguments["task_id"]
    task = await db.get_task(task_id)
    if not task:
        return {"error": f"Task '{task_id}' not found"}

    attempt = arguments.get("attempt")
    project = await db.get_project(task["project_id"]) if task.get("project_id") else None
    log_dir, source = _resolve_log_dir(task, project, attempt)

    if not log_dir:
        return {"error": "No log data found (no live worktree and no archived attempts)"}

    log_path = os.path.join(log_dir, "dispatch.log")
    if not os.path.isfile(log_path):
        return {"text": "", "message": "No dispatch log file found", "source": source}

    tail = arguments.get("tail", 20)
    try:
        with open(log_path) as f:
            lines = f.readlines()
        text = "".join(lines[-tail:])
    except Exception as e:
        return {"error": f"Failed to read dispatch log: {e}"}

    return {"text": text, "source": source}


async def _handle_list_attempts(arguments):
    return await tasks.list_attempts(arguments["task_id"])


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


async def _handle_get_pipeline(arguments):
    chain = await db.get_chain(arguments["task_id"])
    current_idx = next((i for i, t in enumerate(chain) if t["id"] == arguments["task_id"]), -1)
    return {"chain": chain, "current_index": current_idx}


async def _handle_search_task_messages(arguments):
    return await db.search_task_messages(
        query=arguments["query"],
        project_id=arguments.get("project_id"),
        limit=arguments.get("limit", 20),
    )


async def _handle_create_component(arguments):
    component_id = arguments.pop("id")
    project_id = arguments.pop("project_id")
    name = arguments.pop("name")
    return await db.create_component(
        id=component_id, project_id=project_id, name=name, **arguments,
    )


async def _handle_update_component(arguments):
    component_id = arguments.pop("id")
    if not arguments:
        return {"error": "No fields to update"}
    return await db.update_component(component_id, **arguments)


async def _handle_get_component(arguments):
    result = await db.get_component(arguments["id"])
    return result if result else {"error": f"Component '{arguments['id']}' not found"}


async def _handle_list_components(arguments):
    return await db.list_components(project_id=arguments.get("project_id"))


async def _handle_pause_component(arguments):
    return await tasks.pause_component(arguments["component_id"])

async def _handle_resume_component(arguments):
    return await tasks.resume_component(arguments["component_id"])

async def _handle_stop_component(arguments):
    return await tasks.stop_component(arguments["component_id"])

async def _handle_pause_project(arguments):
    return await tasks.pause_project(arguments["project_id"])

async def _handle_resume_project(arguments):
    return await tasks.resume_project(arguments["project_id"])

async def _handle_stop_project(arguments):
    return await tasks.stop_project(arguments["project_id"])


async def _handle_link_conversation(arguments):
    return await db.link_conversation(
        component_id=arguments["component_id"],
        conversation_id=arguments["conversation_id"],
    )


async def _handle_unlink_conversation(arguments):
    return await db.unlink_conversation(
        component_id=arguments["component_id"],
        conversation_id=arguments["conversation_id"],
    )


async def _handle_search_component(arguments):
    return await db.search_component(
        component_id=arguments["component_id"],
        query=arguments["query"],
        include_graphiti=arguments.get("include_graphiti", False),
        limit=arguments.get("limit", 20),
    )


GUIDE_STATIC = """# Switchboard Guide

## What is Switchboard?

Switchboard is an async task orchestration system for Claude Code sessions. Think of it as a **PM/tech lead layer** that dispatches work to autonomous CC agents, monitors their progress, and manages a quality gate pipeline (test → review → PR).

### Mental Model
- **You** (PM/tech lead) define specs, create tasks, and monitor progress
- **CC workers** execute tasks in isolated git worktrees with full autonomy
- **Gate pipeline** automatically runs tests, dispatches reviews, and creates PRs
- **Components** group related tasks and provide config inheritance

## Available Tools by Workflow

### Planning & Setup
| Tool | Purpose |
|---|---|
| `create_project` | Register a repo with working dir, setup commands, test commands |
| `create_component` | Group tasks under a feature/epic with config inheritance |
| `create_conversation` | Start a design conversation (specs, plans, Q&A) |

### Dispatching Work
| Tool | Purpose |
|---|---|
| `dispatch_task` | Create a task and launch a CC session (non-blocking) |
| `resume_task` | Resume a paused task with the same session (preserves context) |
| `retry_task` | Start a fresh session (injects review feedback if posted) |

### Monitoring
| Tool | Purpose |
|---|---|
| `get_task_status` | Full task status: checklist, messages, artifacts, liveness |
| `list_tasks` | List tasks with filters (project, status, tag, component) |
| `get_session_log` | CC's tool calls and text output (JSONL) |
| `get_dispatch_log` | Dispatch metadata, cost, timing |
| `get_pipeline` | View the full dependency chain for a task |

### Communication
| Tool | Purpose |
|---|---|
| `post_task_message` | Post to a task's message thread |
| `read_task_messages` | Read messages (cursor-based polling) |
| `search_task_messages` | Full-text search across all task messages |

### Conversations (async message board)
| Tool | Purpose |
|---|---|
| `board` | Dashboard of active conversations |
| `post` | Post to a conversation |
| `read` | Read messages (cursor-based) |
| `get_pinned` | Get the source-of-truth pinned message |

## Common Patterns

1. **Starting a feature**: `create_component` → `dispatch_task` with `component_id` and `depends_on`
2. **Task chains**: Use `depends_on` to create sequential pipelines — next task auto-dispatches when gate passes
3. **Review workflow**: Post feedback with `post_task_message(type='review')`, then `retry_task` — feedback is auto-injected
4. **Resuming work**: Use `resume_task` to continue with the same session context (preserves CC's memory)
5. **Config inheritance**: Project → Component → Task. Set `model`, `auto_test`, etc. at any level

### Components & Punchlist
| Tool | Purpose |
|---|---|
| `create_component` | Group tasks under a feature/epic with config inheritance |
| `update_component` | Update component config, status, description |
| `list_components` | List components, optionally filtered by project |
| `link_conversation` | Link a design conversation to a component |
| `add_punchlist_item` | Add a lightweight issue to a component's punchlist |
| `list_punchlist` | View open punchlist items for a component |
| `claim_punchlist_item` | CC claims a punchlist item during work (resolved on gate pass, reverted on failure) |
| `search_component` | Full-text search across a component's conversations and task messages |

### Bulk Operations & Migration
| Tool | Purpose |
|---|---|
| `update_task` | Update any mutable task field (status, model, retry_after, component_id, etc.) |
| `bulk_update_tasks` | Update multiple tasks at once with filters |
| `move_task` | Move a task to a different project or component |

### Control (Pause/Stop/Resume)
| Tool | Purpose |
|---|---|
| `pause_component` | Pause — no new tasks dispatched, running tasks finish |
| `stop_component` | Pause + cancel all running tasks immediately |
| `resume_component` | Resume a paused component |
| `pause_project` | Pause entire project |
| `stop_project` | Pause + cancel all running tasks in project |
| `resume_project` | Resume a paused project |

## Pipeline Features

### Auto-Merge
Set `auto_merge=true` on dispatch. When gate passes: merge branch into target → auto-release worktree → advance chain. Chain-aware: child merges into parent branch, falls back to main when parent already merged.

### Crash Recovery
Three-layer self-healing:
1. **Graceful shutdown** — marks working tasks for recovery before service stops
2. **Signal detection** — SIGTERM/SIGKILL keeps tasks as "working" not "failed"
3. **Health check** (every 60s) — finds dead PIDs, orphaned tasks, stalled chains, rate-limited tasks past retry time

### Rate Limiting
When CC hits usage limits, the task is parked as `rate-limited` with a `retry_after` timestamp parsed from the error message. The health check auto-dispatches it when limits reset. You can also set `retry_after` manually on any task for custom backoff.

### Punchlist Integration
Punchlist items are claimed by CC during work (`claim_punchlist_item`). Items only resolve when the claiming task passes the full gate pipeline. If the task fails or is retried, claimed items revert to open. This prevents false completions.

## Common Patterns

1. **Starting a feature**: `create_component` → `dispatch_task` with `component_id` and `depends_on`
2. **Task chains**: Use `depends_on` to create sequential pipelines — next task auto-dispatches when gate passes
3. **Review workflow**: Post feedback with `post_task_message(type='review')`, then `retry_task` — feedback is auto-injected
4. **Resuming work**: Use `resume_task` to continue with the same session context (preserves CC's memory)
5. **Config inheritance**: Project → Component → Task. Set `model`, `auto_test`, etc. at any level
6. **Auto-merge chains**: Set `auto_merge=true` on all tasks in a chain — they merge sequentially as each passes
7. **Delayed dispatch**: Set `retry_after` on a task to schedule it for a specific time
8. **Kill switch**: `stop_component` or `stop_project` to immediately halt all work

## Anti-Patterns

- **Don't write the implementation plan** — CC does that during its grounding phase
- **Don't micromanage** — give clear specs and let CC work autonomously
- **Don't use retry when you mean resume** — retry clears the session and starts fresh; resume continues
- **Don't skip the spec** — tasks without specs produce worse results
- **Don't set auto_test=false** unless you have a good reason — the gate catches most issues
- **Don't use pkill/kill in CC sessions** — CC runs in a process group and will terminate itself
- **Don't set auto_merge and auto_pr on the same task** — they're mutually exclusive
"""


# ---------------------------------------------------------------------------
# Punchlist tool handlers
# ---------------------------------------------------------------------------

async def _handle_add_punchlist_item(arguments):
    result = await db.add_punchlist_item(
        component_id=arguments["component_id"],
        item=arguments["item"],
        author=arguments.get("author"),
    )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def _handle_list_punchlist(arguments):
    items = await db.list_punchlist(
        component_id=arguments["component_id"],
        include_done=arguments.get("include_done", False),
        claimed_by=arguments.get("claimed_by"),
    )
    return [TextContent(type="text", text=json.dumps(items, indent=2))]


async def _handle_claim_punchlist_item(arguments):
    result = await db.claim_punchlist_item(
        item_id=arguments["item_id"],
        task_id=arguments["task_id"],
    )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def _handle_resolve_punchlist_item(arguments):
    item = await db.get_punchlist_item(arguments["item_id"])
    if not item:
        raise ValueError(f"Punchlist item {arguments['item_id']} not found")
    result = await db.update_punchlist_item(
        item_id=arguments["item_id"],
        status="done",
        resolved_by=arguments["task_id"],
        resolved_at=db.now_iso(),
    )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def _handle_get_context(arguments):
    """Lightweight orientation snapshot — call first in every conversation."""
    projects = await db.list_projects()
    task_counts = await db.get_project_task_counts()
    active_count = await db.count_active_tasks()

    # Project summaries
    project_lines = []
    for p in projects:
        counts = task_counts.get(p["id"], {})
        total = counts.get("total_tasks", 0)
        active = counts.get("active_task_count", 0)
        cost = counts.get("total_cost", 0)
        components = await db.list_components(project_id=p["id"])
        comp_str = f", {len(components)} components" if components else ""
        project_lines.append(f"  - {p['id']}: {total} tasks ({active} active), ${cost:.2f}{comp_str}")

    # Active/blocked tasks
    active_tasks = await db.list_tasks(status="working")
    blocked_tasks = await db.list_tasks(status="needs-review")
    rate_limited = await db.list_tasks(status="rate-limited")

    task_lines = []
    for t in (active_tasks or [])[:5]:
        phase = f" [{t.get('phase', '')}]" if t.get("phase") else ""
        task_lines.append(f"  - {t['id']}{phase} — {(t.get('goal') or '')[:60]}")
    for t in (blocked_tasks or [])[:3]:
        task_lines.append(f"  - {t['id']} [needs-review] — {(t.get('goal') or '')[:60]}")
    for t in (rate_limited or [])[:3]:
        task_lines.append(f"  - {t['id']} [rate-limited] — {(t.get('goal') or '')[:60]}")

    # Recent significant events
    events = await db.get_recent_activity(limit=5)
    event_lines = []
    for ev in events:
        task_short = ev.get("task_id", "").split("/")[-1] if ev.get("task_id") else ""
        title = ev.get("title") or ev.get("event_type", "")
        event_lines.append(f"  - [{ev.get('created_at', '')[:16]}] {task_short}: {title}")

    # Pinned conversations
    convs = await db.list_conversations()
    pinned_convs = [c for c in convs if c.get("has_pinned")]

    parts = [
        f"# Switchboard Context",
        f"",
        f"**Projects:** {len(projects)} | **Active tasks:** {active_count}",
        f"",
    ]

    if project_lines:
        parts.append("## Projects")
        parts.extend(project_lines)
        parts.append("")

    if task_lines:
        parts.append("## Active / Attention Needed")
        parts.extend(task_lines)
        parts.append("")

    if event_lines:
        parts.append("## Recent Events")
        parts.extend(event_lines)
        parts.append("")

    if pinned_convs:
        parts.append("## Conversations with Pinned Context")
        for c in pinned_convs[:10]:
            parts.append(f"  - `{c['id']}`: {c.get('goal', '')[:80]}")
        parts.append("")

    parts.append("_Call `get_guide` for the full tool reference. Use `conversations(search=...)` to find prior context._")

    return {"context": "\n".join(parts)}


async def _handle_get_guide(arguments):
    """Return the Switchboard guide with live system summary appended."""
    parts = [GUIDE_STATIC]

    # Live system summary
    projects = await db.list_projects()
    task_counts = await db.get_project_task_counts()
    active_count = await db.count_active_tasks()

    # Count components
    component_count = 0
    for p in projects:
        components = await db.list_components(project_id=p["id"])
        component_count += len(components)

    parts.append("## Live System Summary\n")
    parts.append(f"- **Projects**: {len(projects)}")
    parts.append(f"- **Active tasks**: {active_count}")
    parts.append(f"- **Components**: {component_count}")
    parts.append("")

    if projects:
        parts.append("### Projects")
        for p in projects:
            counts = task_counts.get(p["id"], {})
            total = counts.get("total_tasks", 0)
            active = counts.get("active_task_count", 0)
            cost = counts.get("total_cost", 0)
            parts.append(f"- **{p['id']}**: {total} tasks ({active} active), ${cost:.2f} total cost")

    return {"guide": "\n".join(parts)}


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
    "release_worktree": _handle_release_worktree,
    "resume_task": _handle_resume_task,
    "retry_task": _handle_retry_task,
    "cancel_task": _handle_cancel_task,
    "approve_task": _handle_approve_task,
    "close_task": _handle_close_task,
    "get_task_status": _handle_get_task_status,
    "list_tasks": _handle_list_tasks,
    "update_task": _handle_update_task,
    "bulk_update_tasks": _handle_bulk_update_tasks,
    "move_task": _handle_move_task,
    "update_task_checklist": _handle_update_task_checklist,
    "update_task_phase": _handle_update_task_phase,
    "post_task_message": _handle_post_task_message,
    "read_task_messages": _handle_read_task_messages,
    "get_session_log": _handle_get_session_log,
    "get_dispatch_log": _handle_get_dispatch_log,
    "list_attempts": _handle_list_attempts,
    "add_checklist_item": _handle_add_checklist_item,
    "remove_checklist_item": _handle_remove_checklist_item,
    "update_checklist_item": _handle_update_checklist_item_text,
    "get_pipeline": _handle_get_pipeline,
    "search_task_messages": _handle_search_task_messages,
    # Component tools
    "create_component": _handle_create_component,
    "update_component": _handle_update_component,
    "get_component": _handle_get_component,
    "list_components": _handle_list_components,
    "link_conversation": _handle_link_conversation,
    "unlink_conversation": _handle_unlink_conversation,
    # Punchlist tools
    "add_punchlist_item": _handle_add_punchlist_item,
    "list_punchlist": _handle_list_punchlist,
    "claim_punchlist_item": _handle_claim_punchlist_item,
    "resolve_punchlist_item": _handle_resolve_punchlist_item,
    # Pause/Stop/Resume
    "pause_component": _handle_pause_component,
    "resume_component": _handle_resume_component,
    "stop_component": _handle_stop_component,
    "pause_project": _handle_pause_project,
    "resume_project": _handle_resume_project,
    "stop_project": _handle_stop_project,
    # Ops tools
    "get_context": _handle_get_context,
    "get_guide": _handle_get_guide,
    "search_component": _handle_search_component,
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
        elif path == "/dashboard":
            # Redirect /dashboard → /dashboard/ so the service worker scope (/dashboard/) matches the page URL.
            # Without this, navigator.serviceWorker.ready hangs forever on mobile Chrome (fresh install).
            qs = scope.get("query_string", b"")
            location = b"/dashboard/?" + qs if qs else b"/dashboard/"
            await send({"type": "http.response.start", "status": 302, "headers": [[b"location", location]]})
            await send({"type": "http.response.body", "body": b""})
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
