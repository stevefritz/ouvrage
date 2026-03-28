# Switchboard — Developer Guide for CC Workers

## Dashboard

The dashboard is Foreman. Entry point: `foreman.html` → `dashboard/foreman-app.js` → `dashboard/views/`.
There is NO separate dashboard app.
NEVER create `dashboard/app.js` or `dashboard/index.html` — these are legacy files that were purged.
Foreman views live in `dashboard/views/`. Shared components live in `dashboard/components/`.
If a component in `dashboard/components/` is not imported by any view or `foreman-app.js`, it should not exist.

Switchboard (branded "Foreman" in the dashboard) is an AI task orchestration platform.
It's an MCP server that dispatches Claude Code sessions to work on git repos with isolated
worktrees, test gates, review gates, dependency chains, and crash recovery.

## SAFETY: Running tests and processes

- Use `timeout 60 pytest ...` for targeted test runs — always wrap with timeout
- NEVER use kill, pkill, or killall directly — you WILL terminate yourself
- If a process hangs, let the timeout handle it or escalate to needs-review
- Run targeted tests (specific files/functions) during development, the gate handles the full suite
- If you need to stop a background process, use `timeout` on the original command instead

## Git environment — READ THIS

You are working in a **git worktree**, not a regular clone. Your worktree is linked to a
shared bare repo. This has implications:

- **Do NOT modify git config** — `git config` writes to the shared bare repo config and
  affects all other worktrees. Never run `git config` directly.
- **Pushing works automatically** — a credential helper is pre-configured. Just `git push origin <branch>`.
  Do not set up your own credentials, SSH keys, or remotes.
- **Your remote is HTTPS** — not SSH. Don't change it.
- **Your branch is yours** — commit freely, push when ready. The branch was created for this task.
- **Don't touch other branches** — don't checkout main, don't merge main into your branch,
  don't rebase. Switchboard handles merging after your task passes gates.
- **Don't run `git worktree` commands** — the worktree lifecycle is managed by Switchboard.

If you need to see what's on main, use `git log origin/main` (read-only). Do not checkout or merge it.

## Running tests

```bash
timeout 120 python3 -m pytest tests/ -v --tb=short   # full suite (gate runs this)
timeout 60 python3 -m pytest tests/test_unit.py       # unit tests only
timeout 60 python3 -m pytest tests/test_queue.py      # specific file
timeout 60 python3 -m pytest tests/test_unit.py::TestTailLines  # specific class
```

All tests are async (`pytest-asyncio`, `asyncio_mode=auto`). Use `python3`, not `python`.

## Architecture

```
switchboard/
  server/
    app.py            — Raw ASGI app, route registration, create_app() factory
    tools.py          — MCP tool definitions (70+ tools, pure schema, no logic)
    dispatch.py       — TOOL_HANDLERS dict mapping tool names → handler functions
    context.py        — Request context vars (user_id, is_token_auth, is_worker)
    handlers/         — MCP tool handler implementations, grouped by domain:
      conversations.py, projects.py, tasks.py, components.py,
      punchlist.py, ops.py, tokens.py, common.py
  dispatch/
    engine.py         — Task lifecycle: dispatch, resume, retry, cancel, close, approve
    gates.py          — Test gate, review gate, subtask execution
    sdk_session.py    — CC SDK session management, prompt building, message loop
    queue.py          — FIFO queue drain for concurrency management
    recovery.py       — Crash recovery, orphan detection, stall monitoring
    _state.py         — Shared mutable state (running tasks, active clients)
  db/
    connection.py     — Singleton aiosqlite connection, WAL mode, FK enforcement
    schema.py         — CREATE TABLE statements, migrations
    tasks.py          — Task CRUD, status transitions, checklist ops
    conversations.py  — Conversation + message CRUD, cursor-based pagination
    projects.py       — Project CRUD
    components.py     — Component CRUD
    punchlist.py      — Punchlist item lifecycle
    users.py          — User management, credential encryption (Fernet), API tokens
    search.py         — Semantic search (embedding-based)
    push.py           — Push subscription management
    _helpers.py       — Shared utils: now_iso(), _read_messages(), aggregate queries
  auth/
    middleware.py     — Two-layer auth: session cookies + Bearer JWT, localhost bypass
    oauth.py          — Built-in OAuth 2.0 server (RS256 JWTs, PKCE, refresh rotation)
    sessions.py       — Session cookies, login/logout, rate limiting, Argon2id passwords
  git/
    worktree.py       — Worktree setup/cleanup (bare clone + per-task worktrees)
    operations.py     — Branch ops, rebase, push, diff, merge, PR creation
    files.py          — File operations utilities
  embeddings/
    service.py        — OpenAI text-embedding-3-small, cosine similarity
    chunks.py         — Message chunking for semantic search
  config/
    settings.py       — Environment variable loading (all config from env)
    constants.py      — Task states, resource limits, review guidance
  models/             — Pydantic-style data models (task, project, component, etc.)
  notifications/
    slack.py          — Per-task Slack threads with rich blocks
    web_push.py       — VAPID-signed browser push notifications
  dashboard/
    api.py            — REST API endpoints for the Foreman SPA
dashboard/              — Foreman SPA (Preact/htm via CDN, no build step)
tests/                  — Pytest suite (876+ tests, async, unit + integration)
```

All code lives in the `switchboard/` package. No root-level Python shims.

## Key patterns

### Raw ASGI — no framework
Routes are manual path matching in `app.py`. No FastAPI, Flask, or Django. Follow the
existing `if path == ...` / `elif path.startswith(...)` pattern when adding routes.

### MCP tools: schema → dispatch → handler
1. **Schema** in `server/tools.py` — `mcp.types.Tool` with `inputSchema` (JSON Schema)
2. **Routing** in `server/dispatch.py` — `TOOL_HANDLERS` dict maps name → async handler
3. **Handler** in `server/handlers/*.py` — `async def _handle_<name>(arguments: dict) → dict`

Handlers are thin wrappers: extract args, call DB/business logic, return a JSON-serializable dict.
Results are wrapped as `TextContent` by `call_tool()` in `app.py`.

### Database access
- All async via `aiosqlite` with a singleton connection (`async with get_db() as db:`)
- Returns `dict`-like `aiosqlite.Row` objects, not ORM models
- WAL journal mode, foreign keys enforced
- Timestamps always ISO format via `now_iso()`
- Credentials (API keys, PATs) encrypted with Fernet before storage

### Request context
Three context vars propagated via asyncio: `user_id`, `is_token_auth`, `is_worker`.
Set in `app.py` per request. Access via `get_request_user_id()` etc. from `server/context.py`.

### Async fire-and-forget
Non-blocking work (embeddings, notifications, last-active touches) uses `asyncio.create_task()`.
These must never block the request path.

## Adding a new MCP tool

1. Define schema in `switchboard/server/tools.py`:
   ```python
   Tool(name="my_tool", description="...", inputSchema={...})
   ```
2. Create handler in appropriate `switchboard/server/handlers/*.py`:
   ```python
   async def _handle_my_tool(arguments: dict) -> dict:
       ...
       return result_dict
   ```
3. Register in `switchboard/server/dispatch.py`:
   ```python
   TOOL_HANDLERS = { ..., "my_tool": _handle_my_tool }
   ```

## Testing — THIS IS CRITICAL

Every new function, endpoint, or behavior MUST have corresponding tests. Test count: **876+**.
It should only go up, never down.

### Fixtures (defined in `conftest.py`)
- `tmp_db` — Temporary SQLite DB with `SWITCHBOARD_DB` env var + Fernet encryption key
- `db` — Initialized DB module (calls `init_db()`), resets singleton on teardown
- `sample_project` — Pre-registered project with env_overrides, model="opus"
- `sample_task` — Task in "working" status with 4 checklist items
- `sample_conversation` — Conversation with 3 messages including pinned spec
- `completed_chain` — 3-task dependency chain, all gate-passed
- `mock_git` — Patches `_run_as_worker`, `setup_worktree`, `cleanup_worktree` as AsyncMock
- `mock_sdk` — Patches `claude_agent_sdk` module with configurable mock agent

### Patterns
- All tests are `async def` — pytest-asyncio with `asyncio_mode=auto`
- Class-based grouping: `class TestFeatureName:` with `@pytest.fixture(autouse=True)` for patches
- DB assertions on dicts: `task = await db.get_task(id); assert task["status"] == "working"`
- Mocking: `AsyncMock()` for async functions, `patch.dict("sys.modules", ...)` for SDK
- Error testing: `pytest.raises(ValueError, match="pattern")`

### Test tiers
- **Unit tests** (`tests/test_*.py`) — Test functions in isolation with mocked DB/git
- **Integration tests** (`tests/test_integration.py`) — Real SQLite DB, real git, no CC sessions

## Auth model

**Two-layer auth, both always active:**

1. **Session auth** — Cookie-based (`switchboard_session`), 7-day TTL + 24h inactivity timeout.
   Used by `/foreman/*` and `/dashboard/api/*`. Login rate-limited (5 fails → 15min lockout).
   Passwords hashed with Argon2id.

2. **Bearer JWT auth** — RS256 JWTs issued by built-in OAuth 2.0 server (authlib).
   1-hour access tokens with `jti` for revocation. 30-day refresh tokens with rotation.
   PKCE S256 support. Used by `/mcp` endpoint.

3. **Localhost bypass** — Requests from `127.0.0.1` / `::1` skip JWT validation entirely.
   This is how CC workers access `/mcp/worker`. Do not change this without understanding
   the full auth flow.

**Unprotected paths:** `/health`, `/.well-known/*`, `/oauth/*`, `/auth/*`, `/foreman/login`

## Database

SQLite, single-file, async via aiosqlite. Schema in `switchboard/db/schema.py`.

**Key tables:**
- `users` — email (unique), password_hash, role, timezone, lockout tracking
- `user_credentials` — Fernet-encrypted API keys (anthropic, github PAT, slack)
- `sessions` — Session cookies with expiry + inactivity tracking
- `oauth_clients`, `oauth_authorization_codes`, `oauth_tokens` — Full OAuth server state
- `projects` — Git repos with test commands, env overrides, model config
- `tasks` — Status, phase, branch, worktree, gate tracking, attempt counters, depends_on
- `task_checklist` — Per-task checklist items
- `components` — Feature groupings within projects, with own config inheritance
- `punchlist` — Tracked items within components (claim/resolve lifecycle)
- `conversations`, `messages` — Persistent threads; messages support cursor-based pagination
- `message_chunks` — Chunked embeddings for semantic search
- `instance` — Single-row instance config (plan tier, owner)
- `push_subscriptions` — Web push endpoints

**FK patterns:** `created_by`, `dispatched_by`, `user_id` all reference `users(id)`.
`task_id` → `tasks(id)`, `project_id` → `projects(id)`, `component_id` → `components(id)`.

## Environment variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `SWITCHBOARD_DB` | No (default: `./data/switchboard.db`) | SQLite database path |
| `SWITCHBOARD_MASTER_KEY` | Yes (auto-generated) | Fernet encryption key for credentials |
| `OAUTH_BASE_URL` | No | Base URL for OAuth endpoints |
| `OAUTH_RSA_KEY_PATH` | No (default: `./data/oauth_rsa_key.pem`) | RSA key for signing JWTs |
| `OAUTH_CLIENT_SECRET` | No (auto-seeded) | claude-mcp OAuth client secret |
| `AUTH_ISSUER_URL` | No | External OAuth issuer (unset = self-issued) |
| `AUTH_AUDIENCE` | No | JWT audience claim |
| `AUTH_REQUIRED_SCOPES` | No | Comma-separated required scopes |
| `WORKER_USER` | No (default: `switchboard`) | OS user for CC worker processes |
| `SLACK_BOT_TOKEN` | No | Enables Slack notifications |
| `SLACK_CHANNEL_ID` | No | Slack channel for task updates |
| `VAPID_PRIVATE_KEY` | No | Web push signing key |
| `VAPID_PUBLIC_KEY` | No | Web push public key |
| `OPENAI_API_KEY` | No | Enables semantic search embeddings |

## Dashboard

The Foreman SPA lives in `dashboard/`. Tech stack:
- **Preact 10.x + htm** loaded via CDN (esm.sh) — no build step, no node_modules
- **Hash-based routing** (`#/board`, `#/task/...`, `#/conversations`)
- **Components** are vanilla JS modules in `dashboard/components/` and `dashboard/views/`
- **REST API** at `/dashboard/api/*` served by `switchboard/dashboard/api.py`
- **Service worker** for web push notifications

Do NOT add a build step, bundler, or node_modules. Keep it CDN-loaded ES modules.

## Visual Verification (Dashboard Tasks)

When working on dashboard UI, use the visual check tool to see what your changes look like:

```bash
python3 scripts/visual-check.py settings              # desktop settings
python3 scripts/visual-check.py settings-mobile        # mobile settings
python3 scripts/visual-check.py landing                # projects page
```

This renders the page with mock data via Playwright and saves a screenshot to /tmp/.
Read the screenshot and compare to the reference image in fixtures/visual/.
Iterate until your output matches the reference.

Adding new pages: add an entry to scripts/visual-config.json and create mock fixtures.

## Things NOT to do

- Don't add frameworks (FastAPI, Flask, Django) — this is raw ASGI by design
- Don't change the MCP Server instance or tool registration pattern
- Don't modify middleware bypass logic without understanding the full auth flow
- Don't store secrets in git, env files, or logs
- Don't skip writing tests — every change needs test coverage
- Don't add a build step to the dashboard — it's CDN-loaded ES modules
- Don't use `kill`/`pkill`/`killall` — a PreToolUse hook blocks these (see `.claude/settings.json`)

## How CC sessions work in this project

1. Task is dispatched → `setup_worktree()` creates isolated git worktree from bare clone
2. CC worker runs in the worktree with access to `/mcp/worker` (localhost bypass)
3. Worker should commit and push to its branch
4. On completion, gate pipeline runs automatically: test gate → review gate
5. Test failures auto-retry (up to `max_gate_retries`) with failure output as feedback
6. Review failures auto-retry with review feedback injected into next session
7. On gate pass, dependent tasks are auto-dispatched (chain progression)
8. Crash recovery detects orphaned tasks on restart and resumes/retries them

## Deployment note

The `.claude/settings.json` in this repo contains a PreToolUse hook that blocks
`kill`/`pkill`/`killall` in CC workers. For the hook to apply to ALL CC workers
(not just those working on this repo), copy it to `~/.claude/settings.json` for
the worker user (`switchboard`) on the VPS:

```bash
cp .claude/settings.json /home/switchboard/.claude/settings.json
```
