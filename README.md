# Ouvrage

An MCP server that turns Claude.ai (or any MCP client) into a dispatch center for autonomous Claude Code agents. Describe work in conversation, dispatch it as a task, and a CC instance picks it up — working in its own git branch, reading code, making changes, committing, and reporting back through the same MCP protocol.

Three ways to interact with running work:

- **Claude.ai** — dispatch tasks, check status, post course corrections, retry, all through natural conversation via MCP tools.
- **Dashboard** — web UI with live session logs, message threads, checklist progress, and direct task actions.
- **Any MCP client** — Claude Code, Cursor, custom agents. CC workers talk back to Ouvrage through the same MCP endpoint that dispatches them.

Workers inherit the user's MCP config, so a task can use tools like `shopify-ai` or `jira` alongside standard code tools. The conversation layer (project threads, task messages, pinned specs) means context from planning carries through execution and back.

## Status

Functional and in daily use. Public under MIT as of 2026-04. Still rough edges in the install surface and a handful of refactors outstanding — see [`docs/internal/pre-oss-architecture-review.md`](docs/internal/pre-oss-architecture-review.md) for an honest self-assessment of the debt.

## Quick start

Requires Docker 24+ and Docker Compose v2.

```bash
git clone https://github.com/stevefritz/switchboard.git ouvrage
cd ouvrage

# 1. Generate a Fernet master key (used to encrypt stored credentials).
mkdir -p secrets
docker run --rm --entrypoint python3 python:3.13-slim -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
  > secrets/master_key

# 2. Build the image.
docker compose -f docker-compose.example.yml build

# 3. Start the stack with an owner bootstrapped on first boot.
#    The OUVRAGE_OWNER_PASSWORD env var is hashed server-side at bootstrap.
OUVRAGE_OWNER_EMAIL=you@example.com \
OUVRAGE_OWNER_PASSWORD=change-me \
docker compose -f docker-compose.example.yml up -d

# 4. Log in.
open http://localhost:8100/dashboard/login
```

That's it. The container auto-generates the OAuth RSA key, initialises the DB, bootstraps your owner user, and starts serving on port 8100.

For compose-file-checked-into-git flows where plaintext in env is a dealbreaker, pre-hash the password with `python3 -m ouvrage hash-password 'change-me'` and set `OUVRAGE_OWNER_PASSWORD_HASH` instead. The hash form takes precedence when both are set.

## Local development

For editing code with live reload (bind-mounted source, no image rebuild on change):

```bash
make docker-dev      # starts docker-compose.dev.yml
# edit files on host
docker compose -f docker-compose.dev.yml restart ouvrage  # pick up changes
```

For running the test suite without Docker:

```bash
make install         # pip install -e '.[dev]' into the current environment
make test            # pytest tests/ -q --tb=short
make test-quick      # --last-failed only
```

CI runs pytest on Python 3.12 and 3.13 on every push and PR (see `.github/workflows/test.yml`).

## Client configuration

### Claude Code (local)

Add the Ouvrage MCP server to `~/.claude.json`:

```json
{
  "mcpServers": {
    "ouvrage": {
      "type": "http",
      "url": "http://localhost:8100/mcp"
    }
  }
}
```

### Claude AI (via OAuth)

The server publishes an OAuth 2.1 authorisation server at `/oauth/*` and issues self-signed RS256 JWTs. When you bootstrap an owner the entrypoint prints the `claude-mcp` OAuth client credentials — save them; you'll paste them into Claude.ai's MCP connector settings.

To re-print the credentials later:

```bash
docker exec ouvrage python3 -c "\
import asyncio, ouvrage.db as db; \
from ouvrage.crypto import decrypt_value; \
async def go(): \
    await db.init_db(); \
    async with db.get_db() as c: \
        r = (await c.execute_fetchall('SELECT client_id, client_secret_encrypted FROM oauth_clients WHERE client_id=\'claude-mcp\''))[0]; \
    print(r['client_id'], decrypt_value(r['client_secret_encrypted'])); \
asyncio.run(go())"
```

### Any MCP client

Any client that speaks the MCP HTTP transport can connect at `http://<host>:8100/mcp` with a Bearer token issued by `/oauth/token`. CC workers connect to `/mcp/worker` which bypasses auth for localhost traffic.

## How it works

Ouvrage dispatches autonomous Claude Code sessions in isolated git worktrees. Each task gets its own branch, its own working directory, and reports progress back through MCP:

1. `dispatch_task` creates a task record and a worktree (bare clone + `git worktree add`).
2. A CC session launches in the background via the Agent SDK — the MCP call returns immediately with the task ID.
3. The worker reads code, edits files, runs tests, commits to its branch. It reports progress via Ouvrage tools (checklist updates, phase changes, messages).
4. When the worker finishes, the branch is auto-pushed to origin.
5. The gate pipeline runs: auto-test → auto-review → PR. Failures retry the session with feedback injected.
6. If the task has dependents, they auto-dispatch from its branch. If `auto_merge` is set, the PR merges.

### Task lifecycle

The database tracks six core states:

```
ready  →  working  →  validating  →  completed
                   ↘              ↙
                     stopped  ←──  (user intervention)
          any state  →  cancelled
```

- **ready** — waiting for dispatch or for a dependency to pass its gate.
- **working** — CC session is active.
- **validating** — gate pipeline running (test or review).
- **stopped** — paused by user action, timeout, error, or gate failure.
- **completed** — finished, gates passed or manually closed.
- **cancelled** — discarded before completion.

The dashboard surfaces richer display states (`testing`, `reviewing`, `needs-review`, `turns-exhausted`, etc.) computed from the DB state plus task flags. See `ouvrage/config/constants.py::CORE_STATE_DEFINITIONS` for the mapping.

All state transitions route through `TaskLifecycle.execute()` in `ouvrage/dispatch/lifecycle.py` — single owner, no side-channel writes.

### Gate pipeline

Optional per-project / per-task, controlled by `auto_test` and `auto_review`:

```
CC completes → auto-push → [auto-test] → [auto-review] → gate passed
                              ↓              ↓
                         test failed    changes requested
                              ↓              ↓
                          auto-retry     auto-retry (limits configurable)
```

- **Auto-test**: runs the project's `test_command` in the worktree. Failure output feeds back into a retry dispatch.
- **Auto-review**: a subtask CC session reviews the diff against the spec. `CHANGES REQUESTED` triggers a retry with the review feedback injected.
- **Auto-PR**: on gate pass (or if no gate is configured), creates a PR on the project's platform (GitHub, GitLab, Bitbucket).
- **Auto-merge**: on `auto_merge`, merges the PR after it lands.

### Task chains

Tasks can depend on each other. A dependent stays `ready` until its dependency's gate passes, then auto-dispatches from the dependency's branch:

```python
dispatch_task(id="add-models")
dispatch_task(id="add-api",      depends_on="add-models")
dispatch_task(id="add-frontend", depends_on="add-api")
```

Chain propagation: if an upstream task retries, downstream tasks are marked `stale`. When the upstream task's gate passes again, stale dependents auto-rebase onto the updated parent branch and re-dispatch with context about what changed.

## MCP tools

~70 tools grouped by domain. A partial reference:

### Conversations

| Tool | Purpose |
|---|---|
| `create_conversation` | Start a project thread. |
| `post` | Post a message (spec, question, answer, plan, note, status). |
| `read` | Read messages with cursor-based pagination (`after=<cursor>` returns only new). |
| `get_pinned` | Fetch the pinned source-of-truth message. |
| `pin` | Pin a message (auto-unpins previous). |
| `conversations` | List/search conversations. |
| `archive` | Archive a resolved conversation. |

### Projects

| Tool | Purpose |
|---|---|
| `create_project` | Register a project for task dispatch. |
| `get_project` / `update_project` / `list_projects` | CRUD. |

`create_project` requires `id`, `repo`, `working_dir`. Optional: `default_branch`, `setup_command`, `teardown_command`, `test_command`, `env_overrides`, `max_turns`, `max_wall_clock`, `claude_md_path`, `auto_test`, `auto_review`, `auto_pr`, `auto_merge`, `review_model`.

### Tasks

| Tool | Purpose |
|---|---|
| `dispatch_task` | Create task + worktree + launch CC session. Non-blocking. |
| `resume_task` | Resume a stopped task; reuses the same SDK session for full history. |
| `retry_task` | Fresh session for a task; optionally clean the worktree first. |
| `cancel_task` | SIGTERM the CC process, mark cancelled, keep the worktree. |
| `close_task` | Mark completed, optionally remove the worktree and delete the branch. |
| `get_task_status` | Checklist, liveness, recent messages, artifacts, token usage. |
| `list_tasks` | Filter by project/status. |

### Worker-side (available to the CC session)

| Tool | Purpose |
|---|---|
| `update_task_checklist` | Check off an item by `item_id`. |
| `update_task_phase` | Update phase (`analysis`, `implementing`, `reviewing`, ...) with detail text. |
| `post_task_message` | Progress updates, questions, results. |
| `read_task_messages` | Cursor-based polling for mid-task injections. |
| `git_push` / `git_fetch` | Authenticated git ops via the platform — workers can't push directly. |

Mid-task message injection: posting to a running task's thread injects the message into the active CC session as a user message at the next safe boundary. Course corrections without stopping the session.

## Dashboard

Served at `http://localhost:8100/dashboard/`. Preact + htm via CDN, no build step.

- Task board with status, phase, cost, checklist progress, gate state, chain visualisation.
- Task detail view with message thread, expandable session log, dispatch log, subtasks.
- Click any log entry to expand the full tool inputs/outputs/results.
- Live updates on a 5-second poll.
- Actions: cancel, retry, resume, close, advance or cancel a chain.

Auth is session cookies (`ouvrage_session`) for browser access, Bearer JWT for the MCP endpoint. Localhost `/mcp/worker` traffic is unauthenticated so in-container CC workers can reach back without credentials.

## Environment variables

Full reference: [`.env.example`](.env.example). The ones that matter:

| Variable | Required | Description |
|---|---|---|
| `OUVRAGE_MASTER_KEY` | Yes | Fernet key encrypting stored credentials. Lose this, lose encrypted data. |
| `OUVRAGE_OWNER_EMAIL` | First boot only | Email for the owner user seeded on startup. |
| `OUVRAGE_OWNER_PASSWORD` | First boot only | Plaintext password, hashed server-side. Or set `_PASSWORD_HASH` with a pre-hashed value. |
| `OUVRAGE_DB` | No | SQLite path (default `/data/ouvrage.db`). |
| `OAUTH_RSA_KEY_PATH` | No | OAuth RSA key path (auto-generated on first boot). |
| `PORT` | No | HTTP port (default `8100`). |
| `WORKER_USER` | No | OS user for worker subprocess isolation (prod only, falls back to current user). |
| `AUTH_MODE` | No | `local` (default) or `saas` (SSO via control plane). |
| `OPENAI_API_KEY` | No | Enables semantic search over conversations. |
| `SLACK_BOT_TOKEN` / `SLACK_CHANNEL_ID` | No | Per-task Slack notifications. |
| `VAPID_PRIVATE_KEY` / `VAPID_PUBLIC_KEY` | No | Browser web-push notifications. |

## Architecture

```
ouvrage/
├── server/           # Raw ASGI app, 70+ MCP tool definitions, per-domain handlers
├── dispatch/         # Task lifecycle state machine, gate pipeline, SDK session mgmt
├── db/               # aiosqlite + WAL, schema migrations, FTS5 + sqlite-vec search
├── auth/             # Session cookies + Bearer JWT, built-in OAuth 2.1 server
├── git/              # Worktree mgmt + GitHub/GitLab/Bitbucket provider abstraction
├── embeddings/       # OpenAI text-embedding-3-small for semantic search
├── notifications/    # Slack threads, web-push
├── config/           # Env-driven settings, task-state constants
└── dashboard/        # REST API for the SPA

dashboard/            # Preact SPA, CDN-loaded ES modules, no build step
tests/                # 2,700+ pytest tests, async, unit + integration
```

- **Raw ASGI**, not FastAPI — one-endpoint MCP surface doesn't need routing sugar.
- **Python 3.12+**, runs on `python:3.13-slim` in production. SQLite via stdlib, `sqlite-vec` for semantic queries.
- **Async throughout** — single-process cooperative, workers are separate OS processes.
- **State machine** — every status change goes through `TaskLifecycle.execute()`; there's no other way.
- **Multi-platform git** — providers for GitHub, GitLab, Bitbucket behind a small ABC.
- **Worker isolation** — production runs worker processes as a dedicated OS user via `setuid` (requires `CAP_SETUID`, `CAP_SETGID`, `CAP_KILL`). Falls back to current-user execution when no worker user is configured.

## Contributing

Issues and PRs welcome. Run `make test` before opening a PR; CI will run it too. Larger changes should come with a short design note — open an issue first to align.

The `CLAUDE.md` at the repo root documents internal conventions (the state-machine contract, the provider interface, test-fixture patterns). Read it before editing `ouvrage/dispatch/` or `ouvrage/git/`.

## License

MIT. See [`LICENSE`](LICENSE).

---

Built by Stephen Fritz.
