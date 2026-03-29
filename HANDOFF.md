# Switchboard/Foreman — New Session Handoff

## What is Switchboard/Foreman?

Switchboard is an AI task orchestration platform. It dispatches autonomous Claude Code workers to isolated git worktrees, manages test/review gates, tracks everything in SQLite, and surfaces a dashboard (branded "Foreman") via a CDN-loaded Preact SPA. It runs as an MCP server on localhost — both Claude.ai and Claude Code workers connect to it.

## First Steps in a New Session

1. Call `get_context` — compact snapshot of active projects, running tasks, recent events.
2. Read `CLAUDE.md` in the repo root for developer guide, architecture overview, and constraints.
3. Search conversations: `conversations(search="topic")` then `read(conversation_id=...)`.
4. Check the pinned message of any relevant conversation — that's the source of truth.

## Key Conversations

| Conversation ID | Description |
|---|---|
| `switchboard-saas` | Foreman core — feature decisions, task log, primary working thread |
| `switchboard-roadmap` | Priorities, planned work, what to build next |
| `platform-spec` | Control plane specs — auth, MCP protocol, API design |
| `system-audit` | Latest codebase audit findings (posted by the system-audit task) |

Search for these: `conversations(search="switchboard")` if IDs have changed.

## Key Components (mcp-switchboard project)

| Component ID | Description |
|---|---|
| `foreman-hardening` | Dashboard stability, visual polish, auth hardening |
| `initial-build` | Original foundational build (historical) |

Components have **zero config at creation** — all fields null, they inherit from the project.

## Dispatch Rules (Read This Before Every Dispatch)

- **Tasks are held by default** (`held=1`). Always explicitly set `held=0` to dispatch immediately, or release manually after review.
- **Never use `gh` CLI directly.** Set `auto_pr=1` on the task/project and Switchboard handles PR creation.
- **Check `pr_status`** on a task before telling Stephen about PR state — don't guess.
- **Dashboard tasks require Playwright screenshots.** Use `python3 scripts/visual-check.py <page>` and attach results with `add_task_file`.
- **No `kill`/`pkill`/`killall`.** A PreToolUse hook blocks these for worker safety.
- **Never modify git config** — writes to the shared bare repo and breaks all worktrees.
- **Never checkout other branches** — your branch is yours. Switchboard handles merging.

## Config Inheritance

Three tiers, first non-null wins:

```
Task override → Component config (JSON) → Project column → System default
```

Key fields and their system defaults:
- `model` = "sonnet"
- `max_turns` = 200
- `max_wall_clock` = 60 minutes
- `auto_test` = 1 (run test gate)
- `auto_review` = 1 (run review gate)
- `auto_pr` = 0 (don't create PRs unless enabled)
- `auto_merge` = 0
- `auto_release_worktree` = 1
- `review_model` = "opus"
- `max_test_retries` = 3
- `max_review_retries` = 2

Resolution function: `_resolve_limit(task_val, project_val, global_default)` in `dispatch/engine.py:71`.

## Where Specs Live

- **Control plane / auth / API design** → `platform-spec` conversation (pinned message)
- **Foreman core / task orchestration** → `switchboard-saas` conversation (pinned message)
- **Priorities / roadmap** → `switchboard-roadmap` conversation (pinned message)
- **Per-task specs** → pinned message in the task's message log (`get_pinned` or `read_task_messages`)

## How to Regenerate This Doc

1. Dispatch `system-audit` task (use opus, analysis only, no code changes):
   ```
   dispatch_task(task_id="mcp-switchboard/system-audit", project_id="mcp-switchboard",
                 goal="Audit the full codebase and post findings", model="opus", held=0)
   ```
2. After it completes, dispatch `system-docs` (depends on system-audit):
   ```
   dispatch_task(task_id="mcp-switchboard/system-docs", project_id="mcp-switchboard",
                 goal="Build interactive architecture doc + HANDOFF.md from audit findings",
                 depends_on="mcp-switchboard/system-audit", held=0)
   ```
3. The docs task builds `dashboard/docs/architecture.jsx` and updates this file.

## Architecture Quick Reference

```
MCP Client → app.py (ASGI) → dispatch.py:TOOL_HANDLERS → handlers/*.py → db.* → JSON

Task lifecycle: dispatch → held? → depends_on? → concurrency? → setup_worktree →
                build_prompt → _run_sdk_session → CC worker → test gate → review gate →
                auto_merge/auto_pr → release_worktree → dispatch dependents

Auth: Session cookie (dashboard) | Bearer JWT (MCP) | Localhost bypass (workers)
DB:   SQLite, aiosqlite, WAL mode, Fernet-encrypted credentials
SPA:  Preact + htm via CDN (esm.sh), no build step, hash routing
```

## Current State (as of system-docs task, 2026-03-29)

### Built and running
- Full MCP server (70+ tools) with session + Bearer JWT auth
- Task dispatch engine with worktree isolation, gate pipeline, crash recovery
- Foreman SPA: project grid, project detail, task detail, conversations, settings, files
- Interactive architecture docs at `#/docs` (this deliverable)
- OAuth 2.0 server (RS256, PKCE, refresh rotation)
- Web push notifications (VAPID)
- Slack per-task threads
- Semantic search (OpenAI embeddings)
- Mobile-responsive dashboard (375px verified)

### Actively being built (check switchboard-roadmap)
- Check `conversations(search="roadmap")` for current priorities

### Key files for orientation
- `/work/mcp-switchboard/CLAUDE.md` — full developer guide
- `/work/mcp-switchboard/dashboard/docs/architecture.jsx` — interactive docs (browse at `#/docs`)
- `/work/mcp-switchboard/switchboard/dispatch/engine.py` — task lifecycle core
- `/work/mcp-switchboard/switchboard/server/tools.py` — all 70+ MCP tool schemas
- `/work/mcp-switchboard/switchboard/db/schema.py` — all 21 database tables
