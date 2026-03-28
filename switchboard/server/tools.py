"""MCP tool definitions — all 70+ tool schemas. Pure data, no logic."""

from mcp.types import Tool

# ---------------------------------------------------------------------------
# Conversation Tools
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
        description=(
            "Get messages from a conversation. Supports pagination (offset/limit), "
            "single-message lookup (message_id), and summary mode for lightweight browsing. "
            "When last_n is set, offset/limit are ignored (backward compat). "
            "Default limit is 50 messages."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Which conversation to read"},
                "message_id": {"type": "integer", "description": "Fetch a single message by ID with full content. When set, all other params are ignored."},
                "after": {"type": "integer", "description": "Cursor: return only messages with id > this value. Use the cursor from a previous read response."},
                "last_n": {"type": "integer", "description": "Return only the N most recent messages (pinned shown at top). When set, offset/limit are ignored."},
                "since": {"type": "string", "description": "ISO timestamp, return messages after this time"},
                "author": {"type": "string", "description": "Filter by author"},
                "type": {"type": "string", "description": "Filter by message type"},
                "pinned_only": {"type": "boolean", "description": "Return only pinned messages", "default": False},
                "offset": {"type": "integer", "description": "Skip this many messages (default 0). Messages ordered by created_at ASC.", "default": 0},
                "limit": {"type": "integer", "description": "Max messages to return (default 50, max 50).", "default": 50},
                "summary": {"type": "boolean", "description": "When true, return lightweight objects with id, title, type, author, created_at, pinned, char_count, preview (first 150 chars). Full content omitted.", "default": False},
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
                "held": {"type": "boolean", "description": "Create task but don't dispatch — requires manual approval first. Use for chain checkpoints. IMPORTANT: If the spec says to hold/wait/pause before dispatching, you MUST set held=true — the spec text alone does nothing. Default: false", "default": False},
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
        name="reopen_task",
        description="Reopen a completed task for revisions. Increments current_attempt, sets status to 'reopened', clears session/gate state, and posts an awaiting-feedback message. After reopening, post feedback via post_task_message, then call start_reopened_task to dispatch CC.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Completed task to reopen"},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="start_reopened_task",
        description="Start a reopened task. Collects feedback messages posted since reopen, rebases onto base branch, invalidates chain dependents, and dispatches CC with feedback as revision instructions. Only callable on 'reopened' tasks.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Reopened task to start"},
                "auto_test": {"type": "boolean", "description": "Override test gate for this attempt only"},
                "auto_review": {"type": "boolean", "description": "Override review gate for this attempt only"},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="approve_task",
        description=(
            "Release a held task for dispatch. Held tasks are checkpoints that won't auto-dispatch "
            "until manually approved. This clears the hold and dispatches the task."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Held task to approve and dispatch"},
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
        description="Task status. Default returns a slim summary (status, phase, gate, cost, last message excerpt). Pass include_detail=true for the full response including test output, resolved config, and all recent messages.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task to check"},
                "include_detail": {"type": "boolean", "description": "Return full detail: all messages, test output, resolved config. Default false returns slim summary.", "default": False},
                "include_log_tail": {"type": "boolean", "description": "Include last 30 lines of CC stdout (only meaningful with include_detail=true)", "default": False},
                "include_full_messages": {"type": "boolean", "description": "When include_detail=true, return full untruncated message content and all checklist fields. Default false returns messages truncated to ~200 chars.", "default": False},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="list_tasks",
        description="List tasks, optionally filtered by project, status, tag, and/or component. By default excludes cancelled tasks and stale error/conflict tasks (active_only=true).",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter to one project"},
                "status": {"type": "string", "description": "Filter by status: ready, working, needs-review, completed, failed, cancelled"},
                "tag": {"type": "string", "description": "Filter by tag"},
                "component_id": {"type": "string", "description": "Filter to one component"},
                "active_only": {"type": "boolean", "description": "Exclude cancelled tasks and stale error/conflict tasks that exhausted retries. Default true.", "default": True},
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
        description=(
            "Read messages from a task's thread. Supports pagination (offset/limit), "
            "single-message lookup (message_id), summary mode, and attempt filtering. "
            "Default limit is 50 messages."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "message_id": {"type": "integer", "description": "Fetch a single message by ID with full untruncated content. When provided, all other params are ignored."},
                "after": {"type": "integer", "description": "Cursor for polling"},
                "last_n": {"type": "integer", "description": "Return only N most recent (pinned at top). When set, offset/limit are ignored."},
                "type": {"type": "string", "description": "Filter by message type"},
                "attempt": {"type": "integer", "description": "Filter to messages from this attempt number only."},
                "offset": {"type": "integer", "description": "Skip this many messages (default 0). Messages ordered by created_at ASC.", "default": 0},
                "limit": {"type": "integer", "description": "Max messages to return (default 50, max 50).", "default": 50},
                "summary": {"type": "boolean", "description": "When true, return lightweight objects with id, title, type, author, created_at, pinned, char_count, preview. Full content omitted.", "default": False},
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
                "held": {"type": "boolean", "description": "Re-hold a ready task to prevent it from dispatching. Only allowed when task status is 'ready'. Use to pause a queued task or undo a premature approval."},
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
    Tool(
        name="list_task_files",
        description=(
            "Browse what CC wrote — list files on a task's branch. Use after task completion to see "
            "what was created or modified before reviewing. Works for any task state (active, completed, "
            "merged, failed). Start with the root to orient, then drill into specific directories. "
            "Avoid recursive=true on large repos — use path to scope instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "path": {
                    "type": "string",
                    "description": "Directory path to list within the repo (e.g. 'src/components'). Omit to list repo root.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "If true, list all files recursively under path. Default: false (one level only).",
                    "default": False,
                },
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="get_task_file",
        description=(
            "Read a specific file from a task's branch — use to verify CC's implementation, "
            "check generated schemas, review code changes, or pull content to discuss with the user. "
            "Binary files are refused. Large files truncated at max_bytes (default 1MB, set lower "
            "for context efficiency). Works for any task state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "path": {"type": "string", "description": "File path within the repo (e.g. 'src/server.py')"},
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum bytes to return. Default: 1048576 (1MB). Set lower for large text files.",
                    "default": 1048576,
                },
            },
            "required": ["task_id", "path"],
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

# ---------------------------------------------------------------------------
# Control Tools
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# RAG Tools
# ---------------------------------------------------------------------------

RAG_TOOLS = [
    Tool(
        name="search_message_chunks",
        description=(
            "Semantic search at the paragraph level within Switchboard messages. "
            "More precise than search_conversations — finds specific sections of long design docs, "
            "prior decisions, or meeting notes rather than surfacing the whole message. "
            "Use when you need a particular passage from a conversation, not just the message that contains it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The semantic search query — describe the specific section you're looking for",
                },
                "conversation_id": {
                    "type": "string",
                    "description": "Optional: scope search to chunks from this specific conversation",
                },
                "project_id": {
                    "type": "string",
                    "description": "Optional: scope search to chunks from this project's conversations and tasks",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum chunk results to return (default 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="search_conversations",
        description=(
            "Semantic search over Switchboard conversation and task messages using embeddings. "
            "Finds relevant messages even when keyword search would miss them — e.g. 'why did we choose SSE' "
            "finds messages about streaming decisions without requiring exact keyword matches. "
            "Results are ranked by cosine similarity weighted by message type (spec/review/note rank higher)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The semantic search query — describe what you're looking for",
                },
                "project_id": {
                    "type": "string",
                    "description": "Optional: scope search to messages from this project's conversations and tasks",
                },
                "conversation_id": {
                    "type": "string",
                    "description": "Optional: scope search to messages from this specific conversation",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (default 5, max 20)",
                    "default": 5,
                },
                "type_filter": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: only return messages of these types (e.g. ['spec', 'note', 'review'])",
                },
            },
            "required": ["query"],
        },
    ),
]


# ---------------------------------------------------------------------------
# API Token Tools
# ---------------------------------------------------------------------------

TOKEN_TOOLS = [
    Tool(
        name="create_api_token",
        description="Create a new API token for authenticating MCP requests. The raw token is returned ONCE — store it securely. Use it as a Bearer token in the Authorization header.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-readable label for this token, e.g. 'my laptop' or 'CI pipeline'"},
            },
        },
    ),
    Tool(
        name="list_api_tokens",
        description="List your API tokens. Never returns the token value or hash — only metadata (id, name, last_used_at, created_at, expires_at).",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="revoke_api_token",
        description="Permanently delete an API token by ID. The token will immediately stop working.",
        inputSchema={
            "type": "object",
            "properties": {
                "token_id": {"type": "integer", "description": "ID of the token to revoke (from list_api_tokens)"},
            },
            "required": ["token_id"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Files Tools
# ---------------------------------------------------------------------------

FILES_TOOLS = [
    Tool(
        name="list_files",
        description="List uploaded files with their absolute paths on disk. CC uses this to discover available reference files. Optionally filter by task_id to get only files attached to a specific task.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Filter files by task ID. When set, returns only files attached to that task.",
                },
            },
        },
    ),
    Tool(
        name="add_task_file",
        description=(
            "Persist a file produced during this task. Pass the absolute path to a file in your worktree. "
            "The file is copied to permanent storage and attached to this task — it will appear in the task's "
            "Files section for download and review. Use this for reports, screenshots, analyses, exports, "
            "or any other output the user should see. Worker endpoint only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The current task ID (from your task context).",
                },
                "source_path": {
                    "type": "string",
                    "description": "Absolute path to the file within your worktree.",
                },
                "filename": {
                    "type": "string",
                    "description": "Display name for the file. Defaults to the source file's basename.",
                },
            },
            "required": ["task_id", "source_path"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Full tools list
# ---------------------------------------------------------------------------

TOOLS = (
    CONVERSATION_TOOLS
    + PROJECT_TOOLS
    + TASK_TOOLS
    + COMPONENT_TOOLS
    + PUNCHLIST_TOOLS
    + OPS_TOOLS
    + CONTROL_TOOLS
    + RAG_TOOLS
    + TOKEN_TOOLS
    + FILES_TOOLS
)
