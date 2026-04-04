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
        description="Start a new conversation on Ouvrage.",
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
            "single-message lookup (message_id), summary mode, and around (jump to a message by ID). "
            "When around is set, conversation_id is resolved automatically — caller only needs the message_id. "
            "When last_n is set, offset/limit are ignored (backward compat). "
            "Default limit is 50 messages."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Which conversation to read. Optional when around is set (resolved automatically)."},
                "around": {"type": "integer", "description": "Jump to a specific message by ID. Returns messages centered on it (use window to control count, default 3). Resolves conversation automatically. When set, all other params are ignored."},
                "window": {"type": "integer", "description": "Number of messages to return centered on the around target (default 3: 1 before + target + 1 after). Pass 5 to get 2 before + target + 2 after. Only used when around is set.", "default": 3},
                "message_id": {"type": "integer", "description": "Fetch a single message by ID with full content. When set, all other params except conversation_id are ignored."},
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
        description="List conversations, optionally filtered by project.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Filter to one project"},
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
            "Register a git repo as an Ouvrage project. Each project gets its own working directory "
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
                    "description": "Required. Default limit on Claude Code conversation turns per task dispatch. Higher = more autonomy, more cost.",
                },
                "max_wall_clock": {
                    "type": "integer",
                    "description": "Required. Default time limit in minutes per task dispatch. Task is paused when exceeded.",
                },
                "model": {
                    "type": "string",
                    "enum": ["sonnet", "opus"],
                    "description": "Required. Default Claude model for tasks. 'sonnet' is faster/cheaper, 'opus' is more capable.",
                },
                "review_model": {
                    "type": "string",
                    "enum": ["sonnet", "opus"],
                    "description": "Required. Default model for self-review subtasks for tasks in this project.",
                },
                "review_ignore_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File glob patterns to exclude from reviewer diffs. Example: ['*.lock', 'vendor/']",
                },
                "auto_test": {
                    "type": ["boolean", "null"],
                    "description": "Required. Project-level default for auto_test gate on dispatched tasks. Overridden per-task.",
                },
                "auto_review": {
                    "type": ["boolean", "null"],
                    "description": "Required. Project-level default for auto_review gate on dispatched tasks. Overridden per-task.",
                },
                "auto_pr": {
                    "type": ["boolean", "null"],
                    "description": "Required. Project-level default for auto_pr on dispatched tasks. Overridden per-task.",
                },
                "auto_merge": {
                    "type": ["boolean", "null"],
                    "description": "Required. Project-level default for auto_merge on dispatched tasks. Overridden per-task.",
                },
                "state_definitions": {
                    "type": "object",
                    "description": "Advanced: custom status colors/labels for dashboard rendering. Most users don't need this.",
                },
                "github_pat_override": {
                    "type": "string",
                    "description": "Optional project-specific GitHub PAT. Encrypted at rest. Use for repos in different orgs or requiring a separate token. If not set, falls back to instance-level PAT.",
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
                "model": {"type": ["string", "null"], "enum": ["sonnet", "opus", None], "description": "Default Claude model for tasks in this project"},
                "review_model": {"type": ["string", "null"], "enum": ["sonnet", "opus", None], "description": "Default model for self-review subtasks for tasks in this project"},
                "review_ignore_patterns": {"type": ["array", "null"], "items": {"type": "string"}, "description": "File glob patterns to exclude from reviewer diffs"},
                "auto_test": {"type": ["boolean", "null"], "description": "Project-level default for auto_test gate"},
                "auto_review": {"type": ["boolean", "null"], "description": "Project-level default for auto_review gate"},
                "auto_pr": {"type": ["boolean", "null"], "description": "Project-level default for auto_pr"},
                "auto_merge": {"type": ["boolean", "null"], "description": "Project-level default for auto_merge"},
                "state_definitions": {"type": ["object", "null"], "description": "Custom state definitions for dashboard rendering"},
                "github_pat_override": {"type": ["string", "null"], "description": "Project-specific GitHub PAT. Pass a new value to encrypt and store. Pass empty string or null to clear (falls back to instance PAT)."},
            },
            "required": ["id"],
        },
    ),
    Tool(
        name="list_projects",
        description="List all registered projects.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="delete_project",
        description=(
            "Delete a project and remove its working directory from disk. "
            "Rejects if the project has tasks in 'working' status — cancel them first. "
            "This is permanent and cannot be undone."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "ID of the project to delete",
                },
            },
            "required": ["project_id"],
        },
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
                "auto_test": {"type": ["boolean", "null"], "description": "Run test_command after completion as a gate. Omit or null to inherit from project default (system default: true)."},
                "auto_review": {"type": ["boolean", "null"], "description": "Dispatch a self-review session after tests pass. Omit or null to inherit from project default (system default: true)."},
                "review_model": {"type": ["string", "null"], "enum": ["sonnet", "opus", None], "description": "Model for self-review task. Omit or null to inherit from project default (system default: opus)."},
                "auto_pr": {"type": ["boolean", "null"], "description": "Auto-create PR when chain tail passes all gates. Omit or null to inherit from project default (system default: false)."},
                "auto_merge": {"type": ["boolean", "null"], "description": "Auto-merge task branch into target on gate pass. Mutually exclusive with auto_pr. Omit or null to inherit from project default (system default: false)."},
                "auto_release_worktree": {"type": ["boolean", "null"], "description": "Auto-detach worktree after gate pass. Omit or null to inherit from project default (system default: true)."},
                "max_test_retries": {"type": ["integer", "null"], "description": "Max test gate retry attempts (system default: 3)."},
                "max_review_retries": {"type": ["integer", "null"], "description": "Max review gate retry attempts (system default: 2)."},
                "base_branch": {"type": "string", "description": "Override merge target branch (defaults to project default_branch)"},
                "depends_on": {"type": "string", "description": "Task ID this depends on. Won't dispatch until parent gate-passes."},
                "claude_chat_url": {"type": "string", "description": "Optional URL linking to the claude.ai chat for this task"},
                "held": {"type": "boolean", "description": "Standalone tasks default to held=true (require approval). Chain tasks (with depends_on) default to held=false. Set explicitly to override."},
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
        name="transition_task",
        description=(
            "Execute a lifecycle action on a task. "
            "Call get_task_status first to see available_actions for the current state.\n\n"
            "Actions: resume, retry, reopen, start, stop, cancel, close, approve, skip_gate\n\n"
            "options dict (action-specific):\n"
            "  close: cleanup (bool, default true) — remove worktree; "
            "force_delete_branch (bool, default false) — git branch -D"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["resume", "retry", "reopen", "start", "stop",
                             "cancel", "close", "approve", "skip_gate"],
                },
                "options": {
                    "type": "object",
                    "description": "Action-specific options. See tool description.",
                    "default": {},
                },
            },
            "required": ["task_id", "action"],
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
        description="List tasks, optionally filtered by project, status, tag, keyword search, date range, with configurable sort and limit. By default excludes cancelled tasks and stale error/conflict tasks (active_only=true).",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Filter to one project"},
                "status": {"type": "string", "description": "Filter by status: ready, working, needs-review, completed, failed, cancelled"},
                "tag": {"type": "string", "description": "Filter by tag"},
                "active_only": {"type": "boolean", "description": "Exclude cancelled tasks and stale error/conflict tasks that exhausted retries. Default true.", "default": True},
                "query": {"type": "string", "description": "Keyword search on task goals using FTS5. Results ranked by BM25 relevance by default."},
                "after": {"type": "string", "description": "Return tasks created after this ISO datetime (e.g. 2026-04-03T00:00:00Z)"},
                "before": {"type": "string", "description": "Return tasks created before this ISO datetime (e.g. 2026-04-04T00:00:00Z)"},
                "limit": {"type": "integer", "description": "Maximum number of tasks to return. Default 50.", "default": 50},
                "sort": {"type": "string", "description": "Sort order: date (last_activity desc, default), created (created_at desc), status (grouped by status), cost (total_cost_usd desc), relevance (BM25 score, auto-selected when query is set)", "enum": ["date", "created", "status", "cost", "relevance"]},
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
            "single-message lookup (message_id), summary mode, attempt filtering, and around (jump to a message by ID). "
            "When around is set, task_id is resolved automatically — caller only needs the message_id. "
            "Default limit is 50 messages."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID. Optional when around is set (resolved automatically)."},
                "around": {"type": "integer", "description": "Jump to a specific message by ID. Returns messages centered on it (use window to control count, default 3). Resolves task automatically. When set, all other params are ignored."},
                "window": {"type": "integer", "description": "Number of messages to return centered on the around target (default 3: 1 before + target + 1 after). Pass 5 to get 2 before + target + 2 after. Only used when around is set.", "default": 3},
                "message_id": {"type": "integer", "description": "Fetch a single message by ID with full untruncated content. When provided, all other params except task_id are ignored."},
                "after": {"type": "integer", "description": "Cursor for polling"},
                "last_n": {"type": "integer", "description": "Return only N most recent (pinned at top). When set, offset/limit are ignored."},
                "type": {"type": "string", "description": "Filter by message type"},
                "attempt": {"type": "integer", "description": "Filter to messages from this attempt number only."},
                "offset": {"type": "integer", "description": "Skip this many messages (default 0). Messages ordered by created_at ASC.", "default": 0},
                "limit": {"type": "integer", "description": "Max messages to return (default 50, max 50).", "default": 50},
                "summary": {"type": "boolean", "description": "When true, return lightweight objects with id, title, type, author, created_at, pinned, char_count, preview. Full content omitted.", "default": False},
            },
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
        name="update_task",
        description="Update task metadata post-dispatch. Use to correct branching info, toggle gates, or update any task field.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID to update"},
                "base_branch": {"type": ["string", "null"], "description": "Correct base branch"},
                "branch_target": {"type": ["string", "null"], "description": "Branch target override"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Replace all tags"},
                "auto_test": {"type": ["boolean", "null"], "description": "Toggle auto-test gate"},
                "auto_review": {"type": ["boolean", "null"], "description": "Toggle auto-review gate"},
                "auto_merge": {"type": ["boolean", "null"], "description": "Toggle auto-merge"},
                "auto_pr": {"type": ["boolean", "null"], "description": "Toggle auto-PR"},
                "max_turns": {"type": ["integer", "null"], "description": "Override turn limit for this task"},
                "max_wall_clock": {"type": ["integer", "null"], "description": "Override wall clock timeout in minutes for this task"},
                "max_test_retries": {"type": ["integer", "null"], "description": "Max test retry attempts"},
                "max_review_retries": {"type": ["integer", "null"], "description": "Max review retry attempts"},
                "model": {"type": ["string", "null"], "enum": ["sonnet", "opus", None], "description": "Override model"},
                "review_model": {"type": ["string", "null"], "enum": ["sonnet", "opus", None], "description": "Override review model for self-review subtask"},
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
        description="Apply the same field updates to multiple tasks in one call. Returns count of updated tasks.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_ids": {"type": "array", "items": {"type": "string"}, "description": "List of task IDs to update"},
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
        description="Full tool reference and workflow guide. Call this when get_context isn't enough — e.g. first time using Ouvrage, or need to understand a specific workflow pattern.",
        inputSchema={"type": "object", "properties": {}},
    ),
]

# ---------------------------------------------------------------------------
# Control Tools
# ---------------------------------------------------------------------------

CONTROL_TOOLS = [
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
# Search Tool
# ---------------------------------------------------------------------------

SEARCH_TOOLS = [
    Tool(
        name="search",
        description=(
            "Search across all Switchboard content — tasks, conversations, messages. "
            "Returns ranked results from task goals, conversation messages, and message chunks. "
            "Use this for any search query: finding prior decisions, locating tasks, "
            "discovering relevant conversations, or searching message history."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for — semantic search, so natural language works well",
                },
                "project_id": {
                    "type": "string",
                    "description": "Optional: scope search to one project",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (default 10, max 30)",
                    "default": 10,
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
        description=(
            "List uploaded/attached files. Each file includes a `readable` flag — true for text formats "
            "(txt, md, json, csv, yaml, xml, toml), false for binary (png, jpg, pdf, etc.). "
            "Use get_file to read content. Optionally filter by task_id or project_id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Filter files by task ID. When set, returns only files attached to that task.",
                },
                "project_id": {
                    "type": "string",
                    "description": "Filter files by project ID. When set, returns only files attached to that project.",
                },
            },
        },
    ),
    Tool(
        name="get_attached_file",
        description=(
            "Deprecated — use get_file instead. "
            "Read the content of a text-based attached file. Only works for readable files "
            "(txt, md, json, csv, yaml, xml, toml) — use list_files to check the `readable` flag first. "
            "Binary files (images, PDFs) are refused. Returns the file content as text."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The file ID from list_files response.",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum bytes to return. Default: 1048576 (1MB).",
                    "default": 1048576,
                },
            },
            "required": ["file_id"],
        },
    ),
    Tool(
        name="add_project_file",
        description=(
            "Persist a file and attach it to a project. Pass the absolute path to a file. "
            "The file is copied to permanent storage and linked to the project — it will appear in the project's "
            "Files section as a persistent reference doc. Worker endpoint only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "The project ID to attach the file to.",
                },
                "source_path": {
                    "type": "string",
                    "description": "Absolute path to the file to persist.",
                },
                "filename": {
                    "type": "string",
                    "description": "Display name for the file. Defaults to the source file's basename.",
                },
            },
            "required": ["project_id", "source_path"],
        },
    ),
    Tool(
        name="get_file",
        description=(
            "Read any file by ID, regardless of scope (task file, project file, or unscoped). "
            "Returns file content for readable text formats (txt, md, json, csv, yaml, xml, toml). "
            "Returns metadata only for binary files (images, PDFs). "
            "Use list_files first to find the file ID and check the `readable` flag."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "The file UUID from list_files response.",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum bytes to return for text files. Default: 1048576 (1MB).",
                    "default": 1048576,
                },
            },
            "required": ["id"],
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
    Tool(
        name="promote_task_file",
        description=(
            "Promote a task file to project scope. Sets project_id on an existing file that already has a task_id. "
            "After promotion the file appears in both the task's files and the project's files. "
            "Use this to surface important task artifacts as persistent project reference docs."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The file UUID to promote.",
                },
                "project_id": {
                    "type": "string",
                    "description": "The project ID to associate the file with.",
                },
            },
            "required": ["file_id", "project_id"],
        },
    ),
]

# ---------------------------------------------------------------------------
# Worker-only tools (available exclusively on /mcp/worker endpoint)
# ---------------------------------------------------------------------------

WORKER_TOOLS = [
    Tool(
        name="escalate",
        description=(
            "Flag a task for human review. Sets the task to needs-review status and posts "
            "a message explaining why. Use this when you encounter a problem you cannot resolve "
            "— ambiguous spec, blocking issue, or something outside your scope. "
            "Do not try to work around fundamental blockers; escalate instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID to escalate.",
                },
                "reason": {
                    "type": "string",
                    "description": "Clear explanation of why human review is needed.",
                },
            },
            "required": ["task_id", "reason"],
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
    + OPS_TOOLS
    + CONTROL_TOOLS
    + SEARCH_TOOLS
    + TOKEN_TOOLS
    + FILES_TOOLS
)

# Worker allowlist — tools visible to CC workers on /mcp/worker endpoint.
# Any tool not in this set is hidden from list_tools and rejected by call_tool.
WORKER_TOOL_ALLOWLIST = {
    "update_task_phase",
    "update_task_checklist",
    "add_checklist_item",
    "remove_checklist_item",
    "post_task_message",
    "read_task_messages",
    "add_task_file",
    "add_project_file",
    "get_file",
    "promote_task_file",
    "list_files",
    "escalate",
}
