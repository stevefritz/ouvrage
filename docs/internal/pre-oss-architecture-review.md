# mcp-switchboard — Pre-Open-Source Architecture Review

**Date:** 2026-04-19
**Scope:** Full codebase analysis — no code changes
**Codebase size:** ~85 Python source files, ~12,400 lines of application code (excluding tests), ~100 test files, Preact SPA dashboard

---

## 1. Architecture As-Is

### Module / Package Layout

```
switchboard/                     # Core Python package (~12,400 LOC)
  server/                        # MCP server + ASGI app
    app.py            (580L)     — Raw ASGI app, route matching, create_app() factory
    tools.py          (1037L)    — MCP tool schema definitions (~70 tools)
    dispatch.py       (140L)     — TOOL_HANDLERS dict routing tool names → handlers
    context.py        (62L)      — Request-scoped context vars (user_id, is_worker, etc.)
    proxy.py                     — Anthropic API proxy for CC workers
    handlers/                    — MCP tool handler implementations
      tasks.py        (701L)     — Task CRUD, dispatch, checklist, messages, transitions
      conversations.py (347L)    — Conversation + message operations
      files_handler.py (322L)    — File upload/download/promote
      ops.py          (460L)     — get_context, get_guide, search, read
      projects.py     (228L)     — Project CRUD
      search.py       (291L)     — Unified search handler
      git_tools.py    (175L)     — git_push, git_fetch for workers
      tokens.py       (32L)      — API token management
      common.py       (33L)      — Shared handler utils

  dispatch/                      # Task execution engine
    lifecycle.py      (1869L)    — State machine: TRANSITIONS dict, execute(), side effects
    engine.py         (1048L)    — Dispatch orchestration wrappers
    gates.py          (1132L)    — Test gate, review gate, subtask execution
    sdk_session.py    (1023L)    — CC SDK session management, prompt building
    internals.py      (466L)     — Low-level dispatch building blocks
    recovery.py                  — Crash recovery, orphan detection, stall monitoring
    queue.py                     — FIFO queue drain for concurrency
    _state.py         (23L)      — Shared mutable state (running tasks, active clients)
    pr_sweep.py                  — PR merge detection and post-merge cleanup

  db/                            # Database access layer
    __init__.py       (281L)     — Massive re-export facade (100+ symbols)
    schema.py         (1011L)    — CREATE TABLE statements + 40+ migrations
    tasks.py          (947L)     — Task CRUD, status transitions, checklist ops
    search.py         (950L)     — Semantic search, FTS5, hybrid ranking
    conversations.py  (134L)     — Conversation CRUD
    projects.py       (196L)     — Project CRUD
    users.py          (347L)     — User management, credential encryption, API tokens
    files.py          (86L)      — File metadata CRUD
    components.py     (283L)     — Component CRUD
    punchlist.py      (136L)     — Punchlist item lifecycle
    connection.py     (41L)      — Singleton aiosqlite connection, WAL mode
    _helpers.py       (333L)     — Shared utils: now_iso(), pagination, snippets
    git_credentials.py (97L)     — Git credential CRUD
    audit.py          (47L)      — Audit log
    instance_config.py (84L)     — Instance-level config overrides
    push.py           (62L)      — Push notification subscriptions

  auth/                          — Two-layer auth system
    middleware.py     (480L)     — JWT validation, session checking, localhost bypass
    oauth.py          (731L)     — Built-in OAuth 2.0 authorization server
    sessions.py       (351L)     — Session cookies, login/logout, rate limiting
    sso.py            (253L)     — SSO/SAML integration for SaaS mode

  git/                           — Git operations
    worktree.py       (380L)     — Worktree setup/cleanup (bare clone + per-task worktrees)
    operations.py     (471L)     — Branch ops, push, diff, merge
    files.py          (204L)     — File operations utilities
    validation.py     (75L)      — Credential pre-flight checks
    providers/                   — Multi-platform git provider abstraction
      base.py         (73L)      — GitProvider ABC
      github.py       (150L)     — GitHub implementation
      gitlab.py       (246L)     — GitLab implementation
      bitbucket.py    (267L)     — Bitbucket implementation

  config/                        — Configuration
    settings.py       (108L)     — Environment variable reads
    constants.py      (136L)     — Task states, resource limits, review guidance
    nudges.py         (109L)     — Behavioral nudge injection system

  dashboard/
    api.py            (2406L)    — REST API for the Foreman SPA (GOD-MODULE)

  embeddings/                    — Vector search
    service.py                   — OpenAI text-embedding-3-small integration
    chunks.py                    — Message chunking for semantic search

  notifications/                 — External notifications
    slack.py          (377L)     — Per-task Slack threads
    web_push.py       (91L)      — VAPID-signed browser push

  models/                        — Data models (dataclasses, not ORM)
    task.py, project.py, component.py, conversation.py, punchlist.py, checklist.py

  internal/
    api.py            (255L)     — Machine-to-machine endpoints for SaaS control plane

  crypto.py           (55L)      — Fernet encryption helpers
  logging_config.py              — Rotating file logger setup
  migrate.py                     — Auth migration from Authelia
  __main__.py         (79L)      — Entry point: server start, generate-key, migrate-auth

dashboard/                       # Foreman SPA (Preact/htm, CDN-loaded, no build step)
  foreman-app.js                 — App shell and routing
  foreman-shell.js               — Header/layout shell
  router.js                      — Hash-based routing
  views/                         — Page components
  components/                    — Shared UI components
  style.css, sw.js, api.js, tokens.js

tests/                           # ~100 test files, 2700+ tests
```

### Data Flow: Dispatch Loop and Gate Pipeline

1. **Task creation** — User calls `create_task` MCP tool → handler in `tasks.py` → `db.create_task()` → task stored with status `ready`

2. **Dispatch** — User calls `dispatch_task` → `lifecycle.execute(task_id, "dispatch")` → side effect `_dispatch_launch_session()`:
   - Credential pre-flight check (`validate_project_access`)
   - Worktree setup (`setup_worktree()` — bare clone + branch checkout)
   - Prompt assembly (`sdk_session._build_system_prompt()` — massive prompt template)
   - SDK session launch (`_run_sdk_session()` via `claude_agent_sdk`)
   - Status transitions to `working`

3. **CC session runs** — Worker has access to `/mcp/worker` endpoint (localhost bypass, no auth). Worker uses MCP tools to read/write code, post messages, update checklist, push branches.

4. **Completion** — Worker finishes → `lifecycle.execute(task_id, "complete")` → status transitions to `validating`

5. **Test gate** — `gates.py` runs `pytest` against the branch in the worktree. On failure, auto-retries up to `max_test_retries` with failure output injected as feedback. On pass, proceeds to review gate.

6. **Review gate** — An Opus CC session reviews the diff against the spec. On failure, auto-retries with review feedback. On pass, `gate_pass` transition → status `completed`.

7. **Post-gate** — If `auto_pr` is set, creates PR via provider. If `auto_merge`, merges. Dependent tasks auto-dispatch via `_dispatch_dependents()`.

### Concurrency Model

- **Everything is async** — built on `asyncio` with `aiosqlite` for DB access
- **Single-process, cooperative** — one Python process handles all MCP requests, dispatch, gates, recovery
- **Worker isolation** — each CC worker runs as a separate OS process (launched via `claude_agent_sdk`), communicating back via the `/mcp/worker` endpoint
- **Concurrency control** — `queue.py` manages a FIFO queue with configurable `concurrency_limit` (default 6). Tasks queue up when limit is reached.
- **Fire-and-forget** — Non-blocking work (embeddings, notifications, queue drain) uses `asyncio.create_task()`. Background task references are stored in `_state._running_tasks` to prevent GC.
- **Shared mutable state** — `_state.py` holds `_running_tasks`, `_active_clients`, `_running_gates`, `_gate_tasks` as module-level globals. This works for single-process but is fundamentally incompatible with multi-process scaling.

### Persistence Layers

**SQLite (single file, async via aiosqlite):**
- WAL journal mode, foreign keys enforced
- ~20 tables: `users`, `user_credentials`, `sessions`, `oauth_*`, `projects`, `tasks`, `task_checklist`, `task_messages`, `task_artifacts`, `task_attempts`, `task_tags`, `components`, `conversations`, `messages`, `message_chunks`, `punchlist`, `instance`, `instance_config`, `push_subscriptions`, `git_credentials`, `files`, `subtasks`, `audit_log`
- 40+ sequential migrations in `schema.py` (applied inline via `ALTER TABLE` / `CREATE TABLE IF NOT EXISTS`)
- Credentials (API keys, PATs) encrypted with Fernet symmetric encryption

**FTS5 (full-text search):**
- `messages_fts` — full-text index on conversation messages
- `task_messages_fts` — full-text index on task messages
- Used for keyword search across conversations and task history

**sqlite-vec (vector search):**
- `message_chunks` table stores text chunks with embeddings
- `tasks` table has a `goal_embedding` column
- OpenAI `text-embedding-3-small` model for embedding generation
- Cosine similarity search with configurable thresholds
- Hybrid ranking combines FTS5 BM25 scores with vector cosine similarity

**Migrations:**
- No migration framework — migrations are sequential `if`-guarded blocks in `init_db()` in `schema.py`
- Each migration checks for existence before applying (idempotent)
- No migration version tracking — relies on structural checks (column existence, table existence)

### MCP Surface

- **~70 tools** defined in `server/tools.py` as `mcp.types.Tool` objects with JSON Schema
- **Two endpoints**: `/mcp` (user-facing, JWT auth) and `/mcp/worker` (CC workers, localhost bypass)
- **Tool routing**: `TOOL_HANDLERS` dict in `server/dispatch.py` maps tool names → async handler functions
- **Worker-only tools**: some tools check `get_request_is_worker()` and refuse non-worker callers
- **Schema → dispatch → handler** pipeline is clean and consistent
- **Nudge injection**: every tool response gets a random behavioral nudge appended (`_nudge` field)

### File Attachment System

- Files are stored on the local filesystem under `UPLOADS_DIR` (default `/work/.uploads`)
- `db.files` table tracks metadata: `id`, `project_id`, `task_id`, `filename`, `stored_path`, `content_type`, `size_bytes`, `created_at`, `created_by`
- Upload: `files_handler._handle_add_task_file()` copies the file from the worker's worktree to `UPLOADS_DIR/{uuid}/{filename}`
- Download: `files_handler._handle_get_file()` serves the file from `stored_path`
- Promote: `promote_task_file()` copies a task-scoped file to project scope

---

## 2. Structural Quality

### Where the Architecture Holds Up

**Lifecycle state machine (`dispatch/lifecycle.py`)** — This is the crown jewel. The `TRANSITIONS` dict is a clean, declarative state machine with preconditions and side effects. Every status transition goes through `execute()`, making the system's behavior auditable and predictable. The `TransitionDef` dataclass is well-designed with support for dynamic state resolution, preconditions, side effects, labels, and confirm flags.

**MCP tool pipeline (`server/tools.py` → `dispatch.py` → `handlers/*.py`)** — Clean three-layer separation. Schema definitions are pure data, routing is a flat dict, handlers are thin async functions. Easy to add new tools. The pattern is consistent across all ~70 tools.

**Git provider abstraction (`git/providers/`)** — Clean ABC with GitHub, GitLab, and Bitbucket implementations. `resolve_credential()` handles the credential chain (project → instance → legacy) in one place. Each provider implements a small interface (`build_authenticated_url`, `create_pr`, `validate_access`).

**DB access pattern** — Consistent use of `async with get_db() as db:` for all database operations. Helper functions in `_helpers.py` handle common patterns (pagination, timestamp formatting, message reading). The singleton connection with WAL mode is appropriate for SQLite.

**Request context propagation** — `context.py` uses `contextvars.ContextVar` correctly for async request-scoped state. Clean getter/setter interface. Four vars cover all needs.

### Where It Gets Muddy

**`dashboard/api.py` (2406 lines) — God-module.** This is a raw ASGI REST API that handles ALL dashboard endpoints in a single file. It contains route matching, request parsing, response formatting, and business logic. Some handlers are 100+ lines. It should be split into domain-specific modules mirroring the handler structure in `server/handlers/`.

**`dispatch/engine.py` (1048 lines) — Accumulation layer.** Originally thin wrappers around `lifecycle.execute()`, this module has accumulated orchestration logic, PR handling, session fork detection, credential helper recovery, log archival, and more. Functions like `_handle_session_completion()` are doing significant business logic that blurs the boundary between "thin wrapper" and "business logic."

**`dispatch/gates.py` (1132 lines) — Too many concerns.** This module handles test execution, review execution, review prompt building, subtask management, gate retry logic, and result parsing. The review prompt building alone (`_build_review_prompt`, `_build_targeted_review_prompt`, etc.) is a significant subdomain mixed in with gate orchestration.

**`db/__init__.py` (281 lines) — Leaky facade.** Re-exports 100+ symbols including private helpers prefixed with `_` (`_strip_embedding`, `_read_messages`, `_list_with_aggregates`, `_make_snippet`, `_determine_attempt_outcome`). These private symbols are used by test code and other modules, blurring the public API boundary. The `__all__` list duplicates the import list and must be maintained in sync.

**`dispatch/sdk_session.py` (1023 lines) — Mixed concerns.** Combines SDK client management, system prompt construction (massive template strings), message injection, real-time output processing, and session lifecycle. The prompt building is essentially a templating system embedded in Python string concatenation.

**`models/task.py` vs actual state machine** — The `TaskStatus` enum defines 12 values (`BLOCKED`, `TESTING`, `REVIEWING`, `NEEDS_REVIEW`, `TURNS_EXHAUSTED`, `REOPENED`, `MERGED`, `FAILED`, etc.) that are NOT states in the lifecycle's 6-state model (`ready`, `working`, `validating`, `stopped`, `completed`, `cancelled`). These are display states computed by `_effective_ready_reason()` and `constants.CORE_STATE_DEFINITIONS`, but having them in a `TaskStatus` enum implies they're first-class states. This is actively confusing.

**`db/schema.py` (1011 lines)** — All CREATE TABLE statements and all 40+ migrations live in a single function (`init_db()`). No migration framework, no version tracking, no rollback capability. Migrations are sequential `if`-guarded blocks that check for column/table existence before applying. This works but is fragile and hard to audit.

### Layering Discipline

The layering is generally good:
- `server/handlers/` → `dispatch/` → `db/` is clean
- `auth/` is self-contained
- `git/providers/` is clean

But there are violations:
- `dashboard/api.py` bypasses handler layer and calls `db.*` directly
- `internal/api.py` also bypasses handler layer for direct DB access
- `dispatch/gates.py` imports from `dispatch/engine.py` which imports from `dispatch/lifecycle.py` — the dependency graph within `dispatch/` is tangled
- `_state.py` was extracted specifically to break circular imports, which is a symptom of coupling

---

## 3. Cruft & Smells

### Dead Code / Unused Patterns

1. **`models/task.py:TaskStatus` enum** — Many values (`BLOCKED`, `TESTING`, `REVIEWING`, `NEEDS_REVIEW`, `TURNS_EXHAUSTED`, `REOPENED`, `MERGED`, `FAILED`) are display states, not DB states. The enum is rarely used in application code; the lifecycle uses raw strings. Grep for `TaskStatus.` shows minimal usage outside tests.

2. **`models/` package generally underused** — The dataclass models (`Task`, `Project`, `Component`, etc.) exist but the codebase primarily works with `aiosqlite.Row` dicts. The models are defined but the DB layer returns dicts, not model instances. The models serve more as documentation than runtime types.

### Deprecated Aliases Still Present

3. **`github_pat_override`** — Deprecated field on project create/update tools (`server/tools.py:221`, `server/tools.py:270`). Replaced by `credential_override`. Still accepted for backward compatibility but adds confusion.

4. **`get_attached_file`** — Deprecated MCP tool alias for `get_file` (`server/dispatch.py:124`). Both route to the same handler. The deprecated version is still registered and documented in tool schemas.

5. **`server/tools.py:809`** — Tool description explicitly says "Deprecated — use get_file instead."

### Naming Drift

6. **"Foreman" vs "Ouvrage" vs "Switchboard"** — Three names for the same product:
   - "Switchboard" in package names, config vars, systemd services, internal code
   - "Ouvrage" in user-facing text, dashboard branding, tool descriptions, git commit identity
   - "Foreman" in dashboard file names (`foreman.html`, `foreman-app.js`, `foreman-shell.js`), route prefixes (`/foreman`), HTML title was changed to "Ouvrage" but files still say "foreman"
   
   This triple-naming is the most visible cruft. A reader would be confused about which name is canonical.

7. **`held` vs `ready` vs `queued` vs `blocked`** — The lifecycle has 6 states, but the dashboard shows up to 16 display states. The mapping between actual DB status and display status requires understanding `_effective_ready_reason()` in `lifecycle.py` and `CORE_STATE_DEFINITIONS` in `constants.py`. Not documented outside CLAUDE.md.

8. **`gate_retries` vs `max_gate_retries` vs `max_test_retries` vs `max_review_retries`** — Multiple retry count fields that evolved over time. `gate_retries` and `max_gate_retries` are the original; `max_test_retries` and `max_review_retries` were added later for finer control. Both sets still exist on the task.

### God-Modules

9. **`switchboard/dashboard/api.py` (2406 lines)** — Single file containing ALL dashboard REST endpoints. Route matching, request parsing, auth checking, business logic, and response formatting for 30+ endpoints. Should be split by domain.

10. **`switchboard/dispatch/lifecycle.py` (1869 lines)** — Contains the `TransitionDef` dataclass, all side-effect functions (~40 private functions), the `TRANSITIONS` dict (~30 transitions), the `TaskLifecycle.execute()` method, and the `_effective_*` helpers. While the state machine itself is clean, the file is enormous because every side-effect function lives here.

11. **`switchboard/server/tools.py` (1037 lines)** — Pure tool schema definitions, but 70+ tools in one file makes it hard to navigate. Could be split by domain to match handler structure.

### Config Sprawl

12. **Environment variables scattered across files:**
   - `config/settings.py` — 25+ env vars
   - `crypto.py` — `SWITCHBOARD_MASTER_KEY` + Docker secret fallback paths
   - `embeddings/service.py` — `OPENAI_API_KEY` + Docker secret fallback paths
   - `auth/middleware.py` — reads `AUTH_ISSUER_URL` directly
   - `Dockerfile`, `docker-entrypoint.sh` — duplicate env var references
   
   Settings are mostly centralized in `config/settings.py`, but some modules read env vars directly.

13. **Magic numbers in code:**
   - `constants.py:33` — `DEFAULT_MAX_WALL_CLOCK = 60` (minutes, not seconds — unit implicit)
   - `constants.py:34` — `DEFAULT_MAX_CONCURRENT = 6`
   - `constants.py:98` — `STALL_THRESHOLD_SECONDS = 300`
   - `auth/sessions.py:28` — `LOGIN_MAX_ATTEMPTS = 5`
   - `embeddings/service.py:137` — `# Truncate to model token limit (approx 8191 tokens ~ 32K chars)`
   
   These are named constants but scattered across multiple files rather than centralized.

### Migration Scars

14. **Authelia migration residue:**
   - `switchboard/migrate.py` — entire file exists for one-time Authelia → built-in auth migration
   - `switchboard/__main__.py:18-69` — `migrate-auth` subcommand
   - `docs/migration-from-authelia.md` — migration guide with hardcoded personal info
   - `auth/middleware.py:17-18` — comment "legacy Authelia mode for backward compatibility"
   - `auth/middleware.py:140` — "Keep old name as alias for backward compatibility with any call sites"
   - `git/providers/__init__.py:86,109` — "Legacy fallback: instance.github_pat_encrypted (backward compat)"

15. **`db/__init__.py` re-exports deprecated functions:**
   - `get_github_pat`, `get_anthropic_key`, `set_instance_github_pat`, `get_instance_github_pat` — these are pre-provider-abstraction functions that should have been removed when `git_credentials` was added.

16. **`deploy-jonathan.sh`** — References old file structure (`auth.py`, `dashboard_api.py`, `database.py`, `notifications.py`, `server.py`, `tasks.py`, `web_push.py`) that no longer exists. The current codebase uses a `switchboard/` package. This script is non-functional dead code.

### Inconsistent Error Handling

17. **Mixed approaches across layers:**
   - `dispatch/lifecycle.py` — raises `ValueError` for invalid transitions with descriptive messages
   - `server/handlers/tasks.py` — catches exceptions and returns error dicts (`{"error": "..."}`), sometimes `{"isError": true}` (MCP convention)
   - `dashboard/api.py` — uses HTTP status codes (400, 401, 404, 500) with JSON error bodies
   - `internal/api.py` — raw ASGI error responses
   - Some handlers swallow exceptions with `try/except` and log, others propagate

### Inconsistent Logging

18. **Logger naming inconsistency:**
   - Most modules: `logger = logging.getLogger("switchboard.module.name")` (dotted path)
   - Some use `log` instead of `logger` as the variable name
   - `logging_config.py` configures a rotating file handler, but the log directory default is hardcoded to `/opt/switchboard/logs`

---

## 4. First-Reader Experience

### Entry Point

**Partially clear.** `switchboard/__main__.py` makes it runnable via `python -m switchboard`, which is good. But there's no obvious `main.py` or `run.py` at the repo root. The `foreman.html` at root level is the SPA entry point but its purpose isn't immediately clear from the filename. A reader would need to discover the module entry point by reading `pyproject.toml` or guessing.

### File and Module Names

**Mostly self-describing, with notable exceptions:**
- `foreman.html`, `foreman-app.js`, `foreman-shell.js` — "Foreman" is not explained anywhere in the README. It's the dashboard's legacy brand name. A reader would wonder what "Foreman" is.
- `_state.py` — Module name gives no hint of its purpose (shared mutable state for dispatch). The docstring explains it was extracted to break circular imports, which is implementation detail, not purpose.
- `nudges.py` — Non-obvious. A reader would need to read the code to understand this is a behavioral hint system for AI model context.
- `pr_sweep.py` — Name doesn't clearly indicate it handles PR merge detection and post-merge cleanup.

### Mental Model Required

A reader must reconstruct several implicit concepts:

1. **The 6-state vs display-state distinction** — The most critical concept. The DB stores 6 states (`ready`, `working`, `validating`, `stopped`, `completed`, `cancelled`), but the dashboard shows 16+ states. Understanding which are real and which are computed requires reading `lifecycle.py`, `constants.py`, and `_effective_ready_reason()`.

2. **`held` is a flag, not a state** — This is called out in CLAUDE.md but nowhere in the code's public documentation. A reader seeing `held=True` in the DB would naturally assume it's a status value.

3. **Why there are two MCP endpoints** — `/mcp` for users and `/mcp/worker` for CC workers. The localhost bypass auth model is security-critical but not documented outside CLAUDE.md.

4. **The bare repo + worktree pattern** — How Switchboard uses git bare clones with per-task worktrees is fundamental to the architecture but only documented in CLAUDE.md.

5. **What "Ouvrage" vs "Foreman" vs "Switchboard" means** — Three names with no explanation of the relationship.

6. **The nudge system** — Tool responses include a `_nudge` field with behavioral hints. This is invisible from the tool schema and would surprise anyone reading MCP responses.

### Landmines

1. **`os._exit(0)` in `__main__.py:69`** — Used to force-exit after `migrate-auth` because aiosqlite's singleton connection doesn't close cleanly. Correct but jarring.

2. **Circular import break (`_state.py`)** — The existence of this module is a red flag that the dispatch package has coupling issues. A reader following imports would hit this.

3. **`conftest.py` has an `os._exit()` hook** — Tests that leak non-daemon threads cause the test runner to force-exit with a non-zero code. This makes "all tests pass but CI fails" a real scenario that would be extremely confusing to a new contributor.

4. **The `TRANSITIONS` dict supports dynamic state resolution** — `to_state` can be a callable, not just a string. This is powerful but not obvious from reading the dict.

5. **The provider credential chain has three fallback layers** — Project override → instance credential → legacy PAT. Missing any of these layers leads to different failure modes.

---

## 5. Pre-OSS Risks

### Hardcoded Infrastructure (BLOCKERS)

| File | Finding | Severity |
|------|---------|----------|
| `provision-user.sh:36` | Hardcoded VPS IP: `51.222.159.155` | **Blocker** |
| `provision-user.sh:35` | Hardcoded domain: `stephenfritz.dev` | **Blocker** |
| `provision-user.sh:104` | Hardcoded email: `switchboard@stephenfritz.dev` | **Blocker** |
| `provision-user.sh:145` | Hardcoded auth URL: `https://auth.stephenfritz.dev` | **Blocker** |
| `provision-user.sh:180` | Hardcoded path: `/root/infrastructure/authelia/users.yml` | **Blocker** |
| `provision-user.sh:200` | Hardcoded path: `/root/infrastructure/Caddyfile` | **Blocker** |
| `deploy.sh:13-15` | Hardcoded instance paths: `/opt/switchboard*` for prod, test, jonathan | **Blocker** |
| `deploy-jonathan.sh` | Entire file is a personal deploy script for "Jonathan's instance" | **Blocker** |
| `config/settings.py:12` | Default LOG_DIR: `/opt/switchboard/logs` | Risk |
| `config/settings.py:24` | Default UPLOADS_DIR: `/work/.uploads` | Risk |
| `install.sh:66` | Hardcoded migration path: `/root/mcp-switchboard/data/switchboard.db` | **Blocker** |

### Hardcoded Secrets / Credentials

| File | Finding | Severity |
|------|---------|----------|
| `provision-user.sh:285` | OAuth client secret in plaintext: `cd1e49318a854b874862bf73c7e35d3d` | **Blocker** |

**Note:** The crypto module correctly reads the master key from env var or Docker secret — no hardcoded encryption keys in Python code. The OAuth client secret in `provision-user.sh` is the only actual secret found in source.

### Personal / Sensitive Strings

| File | Finding | Severity |
|------|---------|----------|
| `deploy-jonathan.sh:2` | "Deploy switchboard code to Jonathan's instance" | **Blocker** |
| `provision-user.sh:19` | Example with "jonathan" as username | Risk |
| `docs/migration-from-authelia.md:18-19` | Personal email: `stephen@stephenfritz.dev`, name: "Stephen Fritz" | **Blocker** |
| `tests/test_settings_integration.py:271,282` | `"Stephen Fritz"` in test assertions | Risk |
| `tests/test_settings_api.py:107,115` | `"stevefritz"` GitHub username in tests | Risk |
| `tests/test_visual_check.py:81` | `"Stephen Fritz"` in test assertions | Risk |
| `dashboard/components/FormKit.js:237` | `"stevefritz"` in JSDoc example | Minor |
| `fixtures/visual/*.json` | Multiple files reference `stevefritz`, `stephenfritz.dev`, personal repo URLs | **Blocker** |
| `switchboard/dispatch/sdk_session.py:564` | `"Ouvrage Bot <bot@ouvrage.build>"` — email domain | Risk |
| `dashboard/components/Settings.js:391` | Hardcoded URL: `https://ouvrage.build/docs/getting-started` | Risk |
| `dashboard/components/Settings.js:123` | GitHub token URL with `description=Ouvrage` | Minor |

### Single-Tenant Assumptions

| Location | Finding |
|----------|---------|
| `server/app.py:80-83` | MCP endpoint falls back to "instance owner" when no API token provided — single-tenant assumption |
| `_state.py` | Module-level dicts for running tasks/clients — single-process, single-instance assumption |
| `db/connection.py` | Singleton SQLite connection — single-process assumption |
| `queue.py` | In-memory FIFO queue — lost on restart, single-process assumption |
| `internal/api.py` | SaaS mode endpoints exist but are gated behind `AUTH_MODE=saas` — shows multi-tenant was bolted on |
| `config/settings.py:71-87` | `AUTH_MODE`, `CONTROL_PLANE_URL`, `INSTANCE_SLUG` — SaaS scaffolding that won't work for self-hosted users |

### Branding Confusion

The codebase uses three product names inconsistently:
- **"Switchboard"** — Python package name, config variables, systemd services (~200 references)
- **"Ouvrage"** — Dashboard UI title, MCP tool descriptions, git bot identity, domain name (~50 references)
- **"Foreman"** — Dashboard file names, HTML entry point, CSS classes, route prefixes (~30 references)

For OSS publication, one name must be canonical. All three currently coexist.

### Infrastructure Dependencies Readers Can't Replicate

1. **`claude_agent_sdk`** — The SDK for running CC sessions. Import in `_state.py:9`. Unless this is published, the project can't be used.
2. **OpenAI API** — Required for embeddings/semantic search. Degrades gracefully (search falls back to FTS5).
3. **Authelia + Caddy** — Referenced in deploy scripts and migration docs. Not required at runtime.
4. **Systemd** — Deploy scripts assume Linux with systemd.

---

## 6. Top Cleanup Priorities

### 1. Remove / redact personal information and hardcoded secrets
**Why:** Publishing personal VPS IPs, domain names, email addresses, an OAuth client secret, and personal usernames is both a security risk and unprofessional.
**Files:** `provision-user.sh`, `deploy-jonathan.sh`, `deploy.sh`, `install.sh`, `docs/migration-from-authelia.md`, `fixtures/visual/*.json`, test files with `"Stephen Fritz"` / `"stevefritz"`
**Effort:** S
**Classification:** **Blocker**

### 2. Resolve the triple branding (Switchboard / Ouvrage / Foreman)
**Why:** Three names for one product is confusing and signals internal chaos. OSS users need one canonical name.
**Files:** `foreman.html`, `dashboard/foreman-*.js`, `switchboard/server/tools.py`, `switchboard/server/handlers/ops.py`, `switchboard/dispatch/sdk_session.py`, `dashboard/components/Settings.js`, `dashboard/views/LoginView.js`, `dashboard/sw.js`, all test files referencing "Ouvrage"/"Foreman"
**Effort:** M (widespread find-and-replace, but each change is trivial)
**Classification:** **Blocker**

### 3. Remove `deploy-jonathan.sh` and redact `provision-user.sh`
**Why:** `deploy-jonathan.sh` is dead code referencing a non-existent file structure. `provision-user.sh` is infrastructure-specific and contains secrets. Both are operational scripts that don't belong in an OSS repo.
**Files:** `deploy-jonathan.sh` (delete), `deploy.sh` (generalize or delete), `provision-user.sh` (delete or move to `examples/` with all secrets removed)
**Effort:** S
**Classification:** **Blocker**

### 4. Split `dashboard/api.py` (2406 lines)
**Why:** A 2400-line god-module is the first thing a reviewer will flag. It combines route matching, auth, business logic, and serialization for 30+ endpoints. Splitting by domain mirrors the existing `server/handlers/` pattern and makes the codebase navigable.
**Effort:** M
**Classification:** **Nice-to-have** (functional but ugly)

### 5. Clean up `TaskStatus` enum and model/state divergence
**Why:** The `TaskStatus` enum lists 12 values when only 6 are real DB states. The `models/` package defines dataclasses that aren't used at runtime (DB returns dicts). This mismatch would confuse any contributor trying to understand the state model.
**Files:** `switchboard/models/task.py`, `switchboard/config/constants.py`
**Effort:** S (align enum to reality, add docstrings explaining display states)
**Classification:** **Nice-to-have**

### 6. Remove Authelia migration residue
**Why:** One-time migration code and docs for a specific auth system (Authelia → built-in) is irrelevant to OSS users and leaks internal history.
**Files:** `switchboard/migrate.py`, `switchboard/__main__.py` (remove `migrate-auth` subcommand), `docs/migration-from-authelia.md` (delete), `auth/middleware.py` (remove "legacy Authelia" comments)
**Effort:** S
**Classification:** **Nice-to-have**

### 7. Remove deprecated tool aliases and fields
**Why:** `github_pat_override` and `get_attached_file` are deprecated but still registered. They add surface area and confusion.
**Files:** `server/tools.py`, `server/dispatch.py:124`, `server/handlers/files_handler.py:253`, `server/handlers/projects.py`
**Effort:** S
**Classification:** **Nice-to-have**

### 8. Clean up `db/__init__.py` — stop re-exporting private symbols
**Why:** Re-exporting `_strip_embedding`, `_read_messages`, `_list_with_aggregates`, `_make_snippet`, `_determine_attempt_outcome` as public API signals poor encapsulation. Tests that need these should import from the private module directly.
**Files:** `switchboard/db/__init__.py`, test files that import via `db._make_snippet` etc.
**Effort:** S
**Classification:** **Nice-to-have**

### 9. Add a proper README with architecture overview
**Why:** The current README exists but the architecture documentation lives in CLAUDE.md (which is an AI instruction file, not human documentation). An OSS repo needs a proper README explaining what the project is, how to run it, and how it's structured.
**Files:** `README.md` (rewrite), potentially a new `docs/architecture.md`
**Effort:** M
**Classification:** **Blocker**

### 10. Document the `claude_agent_sdk` dependency and runtime requirements
**Why:** The project imports `claude_agent_sdk` (in `_state.py:9`) which is presumably not public. If the SDK isn't available, the project can't function. OSS users need to know what they're getting into.
**Files:** `pyproject.toml`, `README.md`
**Effort:** S
**Classification:** **Blocker**

---

## Summary

mcp-switchboard is a **substantially well-engineered system** that has evolved organically across ~443 tasks. The lifecycle state machine, MCP tool pipeline, and git provider abstraction are genuinely clean. The dispatch loop is robust with proper crash recovery, gate retries, and dependency chains.

The main issues are:
1. **Personal/infrastructure data throughout** — VPS IPs, personal emails, OAuth secrets. Must be scrubbed.
2. **Triple branding confusion** — "Switchboard" vs "Ouvrage" vs "Foreman" needs resolution.
3. **Accumulated deployment scripts** — Dead scripts referencing old file structures and personal instances.
4. **One god-module** — `dashboard/api.py` at 2406 lines is the most visible structural debt.
5. **Model/state divergence** — The `TaskStatus` enum and display state system are confusing.

The core engine is solid and the code quality is generally high. The pre-OSS work is mostly cosmetic scrubbing (personal data, naming consistency, dead code removal) rather than structural rework. The estimated effort is 2-3 days of focused work for the blockers, plus another 2-3 days for nice-to-haves.
