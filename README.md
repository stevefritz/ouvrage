# Ouvrage

An MCP server that turns Claude.ai into a dispatch center for autonomous Claude Code agents. Describe work in conversation, dispatch it as a task, and a CC instance on your VPS picks it up — working in its own git branch, reading code, making changes, committing, and reporting back through the same MCP protocol.

Three ways to interact with running work:

- **Claude.ai** — dispatch tasks, check status, post course corrections, retry — all through natural conversation via MCP tools
- **Dashboard** — web UI with live session logs (every tool call, file read, API response), message threads, checklist progress, and direct task actions
- **Any MCP client** — Claude Code, Cursor, custom agents — anything that speaks MCP can connect and participate. CC workers talk back to Ouvrage through the same MCP endpoint that dispatches them

Workers can have their own MCP servers (loaded from user config), so a task can use tools like Shopify AI or Jira alongside standard code tools. The conversation layer (project threads, task messages, pinned specs) means context from planning carries through to execution and back.

## Deployment

Ouvrage runs as a bare process (not Docker) because the task engine needs host-level access to the `claude` CLI, git, and the filesystem for worktrees.

### Requirements

- Linux VPS (tested on Ubuntu)
- Python 3.10+
- `claude` CLI installed and authenticated (Claude Max subscription)
- `claude-agent-sdk` Python package
- Git

### Manual Install

```bash
# Install deps
pip3 install .

# Create directories
sudo mkdir -p /opt/ouvrage/data /work
sudo chown $USER:$USER /opt/ouvrage /work

# Copy files
cp server.py database.py tasks.py auth.py /opt/ouvrage/

# Run
OUVRAGE_DB=/opt/ouvrage/data/ouvrage.db python3 /opt/ouvrage/server.py
```

### Systemd Service

```bash
sudo cp ouvrage.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ouvrage
```

```bash
# Manage
systemctl status ouvrage
journalctl -u ouvrage -f     # live logs
curl http://localhost:8100/health # health check
```

### Directory Layout

```
/opt/ouvrage/           # Application code
  ├── server.py             # MCP server, ASGI app, tool definitions
  ├── tasks.py              # Task engine — Agent SDK, gate pipeline, subtasks
  ├── database.py           # SQLite models and queries (aiosqlite)
  ├── dashboard_api.py      # REST API for dashboard SPA
  ├── notifications.py      # Slack notifications (outbound only)
  ├── auth.py               # OAuth JWT middleware (Authelia)
  ├── dashboard/            # Static SPA (HTML, JS, CSS)
  └── data/
      └── ouvrage.db    # SQLite database
/work/                      # Task worktrees
  └── {project-id}/
      ├── .bare/            # Bare git clone
      └── {task-id}/        # Worktree per task
```

Server runs on `http://localhost:8100`. Health check at `/health`. MCP endpoint at `/mcp`.

### Why Not Docker?

The task engine dispatches Claude Code sessions via the Agent SDK, which spawns `claude` as a subprocess. That process needs the authenticated CLI, host filesystem access for worktrees, and the ability to manage its own subprocesses. Docker would require privileged mode and host mounts that defeat the purpose of containerization. Deployment is via bare metal with systemd.

## Client Configuration

### Claude Code (local)

```json
// ~/.claude.json
{
  "mcpServers": {
    "ouvrage": {
      "type": "http",
      "url": "http://localhost:8100/mcp"
    }
  }
}
```

### Claude AI (via OAuth on VPS)

Configured through Anthropic's MCP connector with OAuth. See `auth.py` for the resource server middleware.

## Conversation Tools

| Tool | Purpose |
|---|---|
| `board` | Dashboard — show active conversations. Filter by `project`. |
| `create_conversation` | Start a new conversation with slug ID, project, goal. Can include an initial message. |
| `post` | Add a message. Requires `conversation_id`, `author`, `content`. |
| `read` | Get messages. Supports cursor-based polling via `after` param. |
| `get_pinned` | Get the pinned source-of-truth message for a conversation. |
| `pin` | Pin a message by ID (auto-unpins previous). |
| `conversations` | List/search conversations. |
| `archive` | Soft-archive a resolved conversation. |

### Cursor-Based Polling

Avoid flooding context with repeated messages:

```
# First read — get everything
read(conversation_id="my-convo")
→ { messages: [...], cursor: 7 }

# Later — only new messages
read(conversation_id="my-convo", after=7)
→ { messages: [...], cursor: 12 }
```

## Project Management

Before dispatching tasks, register a project. Projects define the repo, environment, and default resource limits.

### Tools

| Tool | Purpose |
|---|---|
| `create_project` | Register a project for task dispatch. |
| `get_project` | Get a project's configuration. |
| `update_project` | Update project settings. |
| `list_projects` | List all registered projects. |

### `create_project` Fields

| Field | Required | Description |
|---|---|---|
| `id` | Yes | Project slug, e.g. `ym-discount-engine` |
| `repo` | Yes | Git repo URL, e.g. `git@github.com:org/repo.git` |
| `working_dir` | Yes | Base path for worktrees, e.g. `/work/ym-discount-engine` |
| `default_branch` | No | Main branch name (default: `main`) |
| `setup_command` | No | Run after worktree creation, e.g. `composer install` |
| `teardown_command` | No | Run on task cleanup |
| `test_command` | No | Command for auto-test gate, e.g. `php artisan test` |
| `env_overrides` | No | Key-value env vars written to `.env.testing` |
| `max_turns` | No | Default max turns per dispatch |
| `max_wall_clock` | No | Default max wall clock minutes per dispatch |
| `claude_md_path` | No | Path to CLAUDE.md relative to repo root |
| `auto_test` | No | Enable automatic test gate after CC completes (default: false) |
| `auto_review` | No | Enable automatic self-review gate (default: false) |
| `auto_pr` | No | Auto-create PR when gate passes (default: false) |
| `review_model` | No | Model for review subtask (default: `opus`) |

## Task Execution System

The task system dispatches autonomous Claude Code sessions that work in isolated git worktrees. Each task gets its own branch, its own working directory, and reports progress back to the Ouvrage server.

### How It Works

1. `dispatch_task` creates a task record, sets up a git worktree (bare clone + `git worktree add`), and runs any project setup commands.
2. A Claude Code session is launched via the Agent SDK in the background — the call returns immediately with the task ID.
3. CC works autonomously: reading code, editing files, running tests, committing to its branch.
4. CC reports progress via Ouvrage MCP tools (checklist updates, phase changes, messages).
5. When CC finishes, the branch is auto-pushed to origin.
6. The gate pipeline runs (test → review → pass), then dependents are dispatched or a PR is created.

### Task Lifecycle

```
ready → working → completed → [gate: testing → reviewing → passed] → dependents dispatched / PR created
                → failed
                → needs-review → working (resume) → completed
                → cancelled
```

- **ready** — Task created, waiting for dependencies or dispatch.
- **working** — CC session is active.
- **completed** — CC finished. Gate pipeline runs next.
- **needs-review** — CC hit a timeout, ran out of turns, or posted a question.
- **failed** — CC session errored out.
- **cancelled** — Manually killed via `cancel_task`.

### Gate Pipeline

After CC completes and its branch is pushed, a configurable gate pipeline runs before downstream tasks dispatch or a PR is created:

```
CC completes → auto-push → [auto-test] → [auto-review] → gate passed → dispatch dependents / auto-PR
                                ↓               ↓
                           test failed     changes requested
                                ↓               ↓
                           auto-retry      auto-retry (up to 2x)
```

**Auto-test**: Runs the project's `test_command` in the task's worktree. If tests fail, CC is re-dispatched with the failure output to fix the issues. Configurable retry limit (`gate_max_retries`, default 2).

**Auto-review**: A subtask runs in the parent's worktree — a fresh CC session that reviews the diff against the original spec. Posts an APPROVED or CHANGES REQUESTED verdict. On rejection, the main task re-dispatches with the review feedback.

**Auto-PR**: When the gate passes (or if no gate is configured), automatically creates a GitHub PR from the task branch.

Gate status values: `testing`, `reviewing`, `passed`, `stale` (chain propagation).

### Task Dependencies & Chains

Tasks can declare dependencies on other tasks. A dependent task stays in `ready` status until its dependency's gate passes, then auto-dispatches — branching from the dependency's branch.

```
dispatch_task(id="add-models", ...)
dispatch_task(id="add-api", depends_on="add-models", ...)
dispatch_task(id="add-frontend", depends_on="add-api", ...)
```

This creates a chain: `add-models → add-api → add-frontend`. Each task branches from its parent's branch, so code flows forward through the chain.

**Chain propagation**: If a task in the chain is retried (e.g., `add-models` needs a fix), all downstream tasks are automatically marked `stale`. When the retried task passes its gate, stale dependents auto-rebase onto the updated parent branch and re-dispatch with context about what changed.

### Subtasks

Subtasks are lightweight CC executions that run inside a parent task's worktree. They don't get their own worktree, don't appear as top-level tasks, and don't trigger the gate pipeline. Currently used for:

- **Review** — a fresh CC session reviews the parent's diff and posts a verdict

Subtasks show in the parent task's detail view via the API (`task.subtasks` array). They track their own token usage and cost, which rolls up to the parent task.

### Push Enforcement

After CC completes, Ouvrage automatically pushes the task branch to origin (with `--force-with-lease`) before the gate pipeline runs. This ensures work is never stranded in an unpushed worktree. The CC prompt also instructs the agent to push, as belt-and-suspenders.

### Dispatch & Lifecycle Tools

| Tool | Purpose |
|---|---|
| `dispatch_task` | Create task + worktree + launch CC. Non-blocking. |
| `resume_task` | Resume a `needs-review` task. Reuses the same session for context preservation. |
| `retry_task` | Start a fresh CC session for a task. Optionally clean worktree (`git checkout .`). |
| `cancel_task` | SIGTERM the CC process, mark as cancelled. Worktree preserved. |
| `close_task` | Mark completed, optionally remove worktree + delete branch. |
| `get_task_status` | Checklist progress, liveness, recent messages, artifacts, token usage. |
| `list_tasks` | List tasks, filter by project and/or status. |

### `dispatch_task` Fields

| Field | Required | Description |
|---|---|---|
| `project_id` | Yes | Which registered project |
| `id` | Yes | Task slug — becomes the branch name |
| `goal` | Yes | One-liner purpose |
| `spec` | No | Full markdown spec (becomes the pinned message) |
| `checklist` | No | Array of checklist item strings |
| `phase` | No | Starting phase: `analysis` or `implementing` (default: `analysis`) |
| `depends_on` | No | Task ID this depends on (chains tasks) |
| `tags` | No | Array of string tags for filtering |
| `max_turns` | No | Turn limit for this dispatch |
| `max_wall_clock` | No | Wall clock timeout in minutes |
| `model` | No | Model override (`opus`, `sonnet`) |
| `auto_test` | No | Override project's auto-test setting |
| `auto_review` | No | Override project's auto-review setting |
| `auto_pr` | No | Override project's auto-PR setting |
| `escalation_criteria` | No | Tells CC when to escalate to human |

### CC-Side Tools

These tools are available to the CC worker session via the Ouvrage MCP connection:

| Tool | Purpose |
|---|---|
| `update_task_checklist` | Mark a checklist item as done/not done by `item_id`. |
| `update_task_phase` | Update the task's phase label and optional detail text. |
| `post_task_message` | Post progress updates, questions, or results to the task thread. |
| `read_task_messages` | Read messages from the task thread (cursor-based polling). |
| `get_task_status` | Read own task status (checklist, artifacts, etc.). |

### Session Persistence

When a task is resumed, the dispatcher reuses the same `session_id`. The Agent SDK picks up the full conversation history, so CC retains context from its previous run. No re-explanation needed — just "check Ouvrage for answers and keep going."

### Resource Limits

Limits resolve in order: task override > project default > global default.

| Limit | Global Default | Description |
|---|---|---|
| `max_turns` | 200 | Maximum Agent SDK turns per dispatch |
| `max_wall_clock` | 60 minutes | Wall clock timeout — task moves to `needs-review` on expiry |
| `max_concurrent` | 3 | Maximum simultaneously running tasks |

### Logging

Each task's worktree contains a `.ouvrage/` directory with:

- `session.jsonl` — Structured session log. Every tool call, result, and message as JSONL entries.
- `dispatch.log` — Dispatch metadata, session IDs, limits, timestamps, result summaries.
- `cc-stderr.log` — Raw stderr from the CC process. Use `get_task_status(include_log_tail=true)` to read the last 30 lines without leaving the board.

### Token & Cost Tracking

Each task tracks cumulative `total_input_tokens`, `total_output_tokens`, and `total_cost_usd` across all dispatches (including resumes and subtasks). Visible in `get_task_status` output.

## Example Workflow

### Simple Task

```
# 1. Register a project
create_project(
  id="ym-discount-engine",
  repo="git@github.com:org/ym-discount-engine.git",
  working_dir="/work/ym-discount-engine",
  setup_command="composer install",
  test_command="php artisan test",
  auto_test=true,
  auto_review=true
)

# 2. Dispatch a task
dispatch_task(
  project_id="ym-discount-engine",
  id="add-bogo-strategy",
  goal="Implement Buy One Get One discount strategy",
  spec="## Requirements\n\n- Create BuyXGetYStrategy class...",
  checklist=["Create strategy class", "Write unit tests", "Add to strategy registry"]
)
→ { task_id: "add-bogo-strategy", status: "working" }

# 3. CC works → completes → branch pushed → tests pass → review approves → gate passed

# 4. Check status
get_task_status(task_id="add-bogo-strategy")
→ { status: "completed", gate_status: "passed", ... }

# 5. Close and clean up
close_task(task_id="add-bogo-strategy", cleanup=true)
```

### Chained Tasks

```
# Task A — add models
dispatch_task(project_id="myapp", id="add-models", goal="Add User and Team models")

# Task B — depends on A, branches from A's branch
dispatch_task(project_id="myapp", id="add-api", goal="Add REST API endpoints", depends_on="add-models")

# Task C — depends on B
dispatch_task(project_id="myapp", id="add-frontend", goal="Add React components", depends_on="add-api")

# A completes + gate passes → B auto-dispatches
# B completes + gate passes → C auto-dispatches
# If A is retried → B and C marked stale → auto-rebase and re-dispatch when A passes again
```

## Mid-Task Message Injection

Post messages to a running task (via `post_task_message` or the dashboard) and they get injected into the active CC session as user messages. The CC worker sees them as course corrections and adjusts its work.

Uses `ClaudeSDKClient` — a persistent bidirectional session — rather than the one-shot `query()` function. A background poller checks the DB every 5 seconds for new messages and calls `client.query()` to inject them at safe conversation boundaries.

## Dashboard

SPA at `/dashboard` (basic auth via Caddy, bypasses OAuth).

- **Task board**: status, phase, cost, checklist progress, gate status, chain visualization
- **Task detail**: message thread, expandable session log, dispatch log, subtasks
- **Session log**: click any entry to expand full tool inputs/outputs/results
- **Live updates**: 5-second polling with scroll pinning
- **Actions**: cancel, retry, resume, close, advance/cancel chain from the UI

### User MCP Servers

CC worker sessions automatically load MCP servers from the worker user's `~/.claude.json`. This means global MCPs (e.g. `shopify-ai`, `shopify-dev-mcp`) are available to tasks without per-project configuration.

## Author Convention

| Author | Who |
|---|---|
| `claude-code` | Claude Code (CLI) |
| `claude-ai` | Claude AI (web/desktop) |
| `cc-worker` | Autonomous CC task worker |
| `dispatcher` | Ouvrage task engine |
| `dashboard` | Dashboard web UI |
| `human` / name | Human operator (freeform) |

## Message Types

Optional, for filtering: `spec`, `plan`, `question`, `answer`, `note`, `review`, `status`, `progress`, `result`, `test-result`

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OUVRAGE_DB` | Yes | Path to SQLite database |
| `OUVRAGE_PORT` | No | Server port (default: 8100) |
| `AUTH_ISSUER_URL` | No | Authelia/OIDC issuer URL (omit to disable OAuth) |
| `RESOURCE_URL` | No | OAuth resource indicator |
| `PUBLIC_HOST` | No | Public hostname for DNS rebinding protection |
| `SLACK_BOT_TOKEN` | No | Slack bot token for outbound notifications |
| `SLACK_CHANNEL_ID` | No | Slack channel for outbound notifications |
| `JIRA_BASE_URL` | No | Base Jira URL for ticket links |

## Architecture

- **Server**: Python + [MCP SDK](https://github.com/modelcontextprotocol/python-sdk), raw ASGI with Streamable HTTP transport
- **Database**: aiosqlite (async SQLite with WAL mode)
- **Task Engine**: `ClaudeSDKClient` from the Agent SDK — persistent bidirectional sessions with mid-task message injection
- **Gate Pipeline**: Auto-test → auto-review (subtask) → auto-PR, with configurable retry
- **Subtasks**: Lightweight CC executions in parent worktree (no separate task/worktree)
- **Chain Propagation**: Automatic invalidation, rebase, and re-dispatch when upstream tasks retry
- **Worktrees**: Bare git clone + `git worktree add` per task for full isolation
- **Dashboard**: Vanilla JS SPA with REST API, real-time polling
- **Auth**: Optional OAuth 2.1 middleware (enabled via `AUTH_ISSUER_URL` env var)
- **Notifications**: Slack outbound only (task dispatched/completed/failed/heartbeat)
- **Deployment**: Bare metal with systemd
