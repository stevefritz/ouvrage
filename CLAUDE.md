# Switchboard — Developer Guide for CC Workers

## NON-NEGOTIABLE CONTRACTS

**1. ALL status changes go through `lifecycle.execute()`. Never call `db.update_task(status=...)` directly.**
The `TaskLifecycle` class in `switchboard/dispatch/lifecycle.py` is the single owner of all task state
transitions. Every status change — dispatch, complete, gate_pass, gate_fail, cancel, resume — must go
through `lifecycle.execute(task_id, action)`. The engine functions in `dispatch/engine.py` are thin
wrappers that call `lifecycle.execute()` internally.

**2. `held` is a boolean flag on `ready` tasks, not a status value. Never set `status='held'`.**
The `held` column is a boolean on the tasks table. A held task has `status='ready'` and `held=True`.
The dashboard displays it as "held" via `_effective_ready_reason()`, but the actual status is `ready`.
To hold/unhold a task, use `lifecycle.execute(task_id, "hold")` / `lifecycle.execute(task_id, "approve")`.

**3. All git operations go through the provider interface. Never call GitHub-specific functions directly.**
The provider abstraction in `switchboard/git/providers/` supports GitHub, GitLab, and Bitbucket.
Use `resolve_credential()`, `provider.build_authenticated_url()`, `provider.create_pr()`,
`provider.validate_access()`. Never import or call platform-specific functions directly.

**4. No leaked threads in tests. Respect conftest.py patterns. All tests must complete within 30s.**
`tests/conftest.py` has a `pytest_unconfigure` hook that detects leaked non-daemon threads and calls
`os._exit()` to force termination. If your test leaks threads, the gate will report exit_code != 0
with all tests passing — extremely hard to debug.

## Dead code warnings — NEVER import or call these

| Deleted function | Replacement |
|-----------------|-------------|
| `_build_authenticated_url()` | `provider.build_authenticated_url()` |
| `create_github_pr()` | `provider.create_pr()` |
| `_find_existing_pr()` | Handled internally by provider |
| `_validate_github_pat_for_repo()` | `validate_project_access()` in `git/validation.py` |

These functions no longer exist anywhere in the codebase. Importing them will cause `ImportError`.

## Lifecycle state machine

### The 6-state model

| Status | Meaning |
|--------|---------|
| `ready` | Task can be dispatched. May also be `held`, `queued`, or `blocked` (display states). |
| `working` | CC session is actively running. |
| `validating` | Gate pipeline running (testing or reviewing). |
| `stopped` | Paused — user action, timeout, error, or gate failure. |
| `completed` | Finished — gates passed or manually closed. |
| `cancelled` | Discarded before completion. |

### Key transitions

```
ready + dispatch    → working     (CC session starts)
ready + approve     → ready       (clears held flag, may auto-dispatch)
working + complete  → validating  (enters gate pipeline)
validating + gate_pass → completed
validating + gate_fail → stopped
stopped + resume    → working     (user resumes)
stopped + start     → working     (user starts after reopen feedback)
completed + reopen  → stopped     (user reopens for revisions)
any + cancel        → cancelled
```

The full transition table is the `TRANSITIONS` dict in `switchboard/dispatch/lifecycle.py`.
Look up `TRANSITIONS[(effective_state, action)]` to find valid actions for any state.

### Display states vs actual states

The `_effective_ready_reason()` function determines what the dashboard shows for `ready` tasks:
- `held=True` → displays as "held"
- In queue → displays as "queued"
- Dependency unmet → displays as "blocked"

Similarly, `validating` tasks show as "testing" or "reviewing" based on which gate is running.

## Provider interface

### Architecture

```
switchboard/git/providers/
  base.py       — GitProvider abstract base class
  __init__.py   — Provider registry, resolve_credential(), get_provider()
  github.py     — GitHub implementation
  gitlab.py     — GitLab implementation
  bitbucket.py  — Bitbucket implementation
```

### Key functions

- **`get_provider(repo_url)`** — Returns the correct `GitProvider` instance for a repo URL
- **`resolve_credential(project)`** — Resolves credential for a project through the chain:
  1. Project-level credential override
  2. Instance-level credential from `git_credentials` table
  3. Legacy fallback: `instance.github_pat_encrypted` (GitHub only)
- **`provider.build_authenticated_url(repo_url, credential)`** — Returns HTTPS URL with embedded auth
- **`provider.create_pr(credential, repo_info, head, base, title, body)`** — Creates a PR on the platform
- **`provider.validate_access(credential, repo_info)`** — Tests if credential can access the repo
- **`normalize_repo_url(url)`** — In `git/operations.py`. Provider-agnostic: converts any git URL format (SSH, HTTP, HTTPS, any host) to canonical `https://host/path.git`

## Credential validation layers

Three layers, running at different times:

1. **Settings test** (informational) — User clicks "Test" in settings UI. Calls `provider.validate_access()`.
   Non-blocking — just shows the user if their credential works.

2. **Project validation** (post-create/update) — When a project is created or updated, `validate_project_access()`
   runs automatically. Result stored on the project. Informational — doesn't block dispatch.

3. **Dispatch pre-flight** (hard gate) — `lifecycle.execute()` calls `validate_project_access()` before
   dispatching. If validation fails or warns, the task is held with reason "credential issue".
   This is the only hard gate — it prevents launching a CC session that can't push.

`validate_project_access()` lives in `switchboard/git/validation.py`.

## Gate pipeline

After a CC session completes (`working` → `validating`):

1. **Test gate** — Runs `timeout 300 python3 -m pytest tests/ -v --tb=short -rFE` against the branch.
   Failures auto-retry up to `max_test_retries` with failure output as feedback.
2. **Review gate** — An Opus instance reviews the diff against the spec.
   Failures auto-retry up to `max_review_retries` with review feedback injected.
3. **PR/Merge** — If `auto_pr` is set, creates a PR via `provider.create_pr()`.
   If `auto_merge` is set, merges automatically on gate pass.
4. **Chain progression** — Dependent tasks auto-dispatch after gates pass.

Gate logic is in `switchboard/dispatch/gates.py`.

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

## Turn economy — EVERY TURN COSTS MONEY

You are a paid worker. Each tool call costs real money. Each 3-minute test run costs ~$0.50 in turns.
Optimize for fewest turns to complete the task correctly.

### Rules

1. **The gate runs the official test suite.** You do NOT need to run the full suite yourself before completing.
   Run targeted tests on the code you changed. The auto-test gate will catch anything you missed.

2. **Never run a command just to get a number for your report.** If tests passed, say "tests pass."
   Do NOT re-run the full suite to report "927 passed." Nobody cares about the count.

3. **Never run the same command twice without changing code between runs.** If it passed, it passed.
   If it failed, fix something before re-running.

4. **Long commands (>60s) get ONE run.** The full test suite takes ~150 seconds. You get one shot
   during development. After that, run only the tests relevant to your changes. The gate handles the rest.

5. **Don't verify things you just did.** If you wrote a file, you don't need to `cat` it back.
   If you ran a command that succeeded, you don't need to run it again to confirm.

6. **Commit and report when done.** Don't run the full suite "one more time to be safe."
   Push your branch, post your result. The gate pipeline exists for verification.

## Git environment — READ THIS

You are working in a **git worktree**, not a regular clone. Your worktree is linked to a
shared bare repo. This has implications:

- **Do NOT modify git config** — `git config` writes to the shared bare repo config and
  affects all other worktrees. Never run `git config` directly.
- **Pushing works via MCP tools** — use `git_push(task_id)` and `git_fetch(task_id)` MCP tools.
  Direct `git push`/`git fetch` are blocked by hooks. Do not set up your own credentials, SSH keys, or remotes.
- **Your remote is HTTPS** — not SSH. Don't change it.
- **Your branch is yours** — commit freely, push when ready. The branch was created for this task.
- **Don't touch other branches** — don't checkout main, don't merge main into your branch,
  don't rebase. Switchboard handles merging after your task passes gates.
- **Don't run `git worktree` commands** — the worktree lifecycle is managed by Switchboard.

If you need to see what's on main, use `git log origin/main` (read-only). Do not checkout or merge it.

## Running tests — READ THIS CAREFULLY

This project has 2700+ tests. Running them with `-v` produces massive truncated output.
Follow this workflow or you WILL waste turns grepping through truncated output.

### The pattern: quiet first, targeted second

```bash
# Step 1: What failed? (quiet summary, fits on screen)
timeout 200 python3 -m pytest tests/ -q --tb=line 2>&1 | tail -40

# Step 2: Why did it fail? (details on ONLY the failures)
timeout 60 python3 -m pytest tests/ --last-failed --tb=short -v

# Step 3: Did my fix work? (re-run just the failures)
timeout 60 python3 -m pytest tests/ --last-failed -v

# Step 4: Full suite green? (one final quiet check)
timeout 200 python3 -m pytest tests/ -q --tb=line 2>&1 | tail -40
```

### Targeted runs during development
```bash
timeout 60 python3 -m pytest tests/test_unit.py -q              # one file
timeout 60 python3 -m pytest tests/test_unit.py::TestTailLines  # one class
timeout 60 python3 -m pytest tests/test_lifecycle.py -k "resume" # keyword match
```

### NEVER do this
- NEVER run `pytest -v` on the full suite — 2700+ PASSED lines will be truncated
- NEVER run the full suite and pipe through `grep FAIL` repeatedly
- NEVER re-run the same test command 3+ times without changing code between runs
- If you catch yourself running pytest with different grep/tail combos, STOP — use `--last-failed`

### Debugging gate failures

If the gate reports exit_code != 0 but all tests show PASSED:

1. **It's NOT a test failure.** Something is killing the process before pytest writes its summary.
2. **Common causes:**
   - **Timeout** — the suite takes ~150s, `timeout 180` barely clears it. If the VPS is under load, it can exceed the limit.
   - **Interactive stdin hang** — a test makes a real git call (push/fetch/clone) without mocking. Git prompts for `Username for 'https://github.com':`, the process hangs, timeout kills it.
   - **Thread leak** — `conftest.py` has an `os._exit()` hook that fires when leaked threads are detected after all tests pass.
3. **How to diagnose:** Run the suite locally and WATCH for hangs. `grep -rn` for unmocked git calls in the test that was most recently added. Check conftest.py for cleanup hooks.
4. **Do NOT re-run the full suite trying to find a failure that doesn't exist.**

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
      conversations.py, projects.py, tasks.py, ops.py, tokens.py,
      common.py, files_handler.py, git_tools.py, search.py
  dispatch/
    engine.py         — Thin wrappers around lifecycle.execute() (dispatch, resume, retry, cancel)
    lifecycle.py      — TaskLifecycle state machine — single owner of ALL status transitions
    internals.py      — Status-agnostic dispatch building blocks (worktree, prompt, SDK launch)
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
    operations.py     — Branch ops, push, diff, merge; normalize_repo_url()
    files.py          — File operations utilities
    validation.py     — validate_project_access() — credential pre-flight checks
    providers/        — Multi-platform git provider abstraction
      base.py         — GitProvider abstract base class
      __init__.py     — Provider registry, resolve_credential(), get_provider()
      github.py       — GitHub implementation
      gitlab.py       — GitLab implementation
      bitbucket.py    — Bitbucket implementation
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
tests/                  — Pytest suite (2700+ tests, async, unit + integration)
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

Every new function, endpoint, or behavior MUST have corresponding tests.
Test count should only go up, never down.

### Fixtures (defined in `conftest.py`)
- `tmp_db` — Temporary SQLite DB with `SWITCHBOARD_DB` env var + Fernet encryption key
- `db` — Initialized DB module (calls `init_db()`), resets singleton on teardown
- `sample_project` — Pre-registered project with env_overrides, model="opus"
- `sample_task` — Task in "working" status with 4 checklist items
- `sample_conversation` — Conversation with 3 messages including pinned spec
- `completed_chain` — 3-task dependency chain, all gate-passed
- `mock_git` — Patches git/subprocess ops: `_run_as_worker`, `setup_worktree`, `cleanup_worktree`, `_ensure_branch_pushed`, `setup_hook_config`, `validate_project_access`
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

### Critical: mock ALL git and network operations

Every test that touches dispatch, lifecycle, gates, or SDK code MUST mock:
- `switchboard.dispatch.engine._run_as_worker` — runs commands as worker user
- `switchboard.dispatch.engine.setup_worktree` — creates real git worktrees
- `switchboard.dispatch.internals.setup_hook_config` — writes .claude/settings.json
- `switchboard.dispatch.engine.cleanup_worktree` — removes worktrees
- `switchboard.git.operations._ensure_branch_pushed` — does real `git push`
- `switchboard.git.validation.validate_project_access` — calls provider API
- `switchboard.dispatch.sdk_session._run_sdk_session` — launches real CC process

Use the `mock_git` fixture from conftest.py, or patch individual functions.

**If you skip a mock, the test will make a REAL call to a git provider.** Git will prompt for
credentials, the test hangs, the gate times out, and exit_code=1 with zero visible failures.
This is extremely hard to debug — the only symptom is "all tests pass but gate says failed."

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

**Key tables:** `users`, `user_credentials` (Fernet-encrypted), `sessions`, `oauth_*` (full OAuth server),
`projects`, `tasks` (status, phase, gates, depends_on), `task_checklist`, `components`, `punchlist`,
`conversations`/`messages` (cursor-based pagination), `message_chunks` (embeddings), `instance`, `push_subscriptions`.

**FK patterns:** `created_by`, `dispatched_by`, `user_id` → `users(id)`.
`task_id` → `tasks(id)`, `project_id` → `projects(id)`, `component_id` → `components(id)`.
Schema in `switchboard/db/schema.py`.

## Environment variables

All config from env vars. Key ones: `SWITCHBOARD_DB` (SQLite path), `SWITCHBOARD_MASTER_KEY` (Fernet key, required),
`OAUTH_BASE_URL`, `OAUTH_RSA_KEY_PATH`, `WORKER_USER` (default: `switchboard`).
Optional integrations: `SLACK_BOT_TOKEN`/`SLACK_CHANNEL_ID`, `VAPID_*` keys, `OPENAI_API_KEY` (embeddings).
Full list in `switchboard/config/settings.py`.

## Dashboard

The Foreman SPA lives in `dashboard/`. Tech stack:
- **Preact 10.x + htm** loaded via CDN (esm.sh) — no build step, no node_modules
- **Hash-based routing** (`#/board`, `#/task/...`, `#/conversations`)
- **Components** are vanilla JS modules in `dashboard/components/` and `dashboard/views/`
- **REST API** at `/dashboard/api/*` served by `switchboard/dashboard/api.py`
- **Service worker** for web push notifications

Do NOT add a build step, bundler, or node_modules. Keep it CDN-loaded ES modules.

## Visual Verification (Dashboard Tasks)

For dashboard UI work, use `python3 scripts/visual-check.py <page>` (e.g. `settings`, `settings-mobile`, `landing`).
Renders via Playwright, saves screenshot to /tmp/. Compare to reference in `fixtures/visual/`.
Config: `scripts/visual-config.json`.

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
