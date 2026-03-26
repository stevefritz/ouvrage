# Switchboard — Developer Guide for CC Workers

This is the MCP Switchboard task dispatch service. It orchestrates Claude Code workers
via the Agent SDK, managing worktrees, gates, and task chains.

## SAFETY: Running tests and processes

- Use `timeout 60 pytest ...` for targeted test runs — always wrap with timeout
- NEVER use kill, pkill, or killall directly — you WILL terminate yourself
- If a process hangs, let the timeout handle it or escalate to needs-review
- Run targeted tests (specific files/functions) during development, the gate handles the full suite
- If you need to stop a background process, use `timeout` on the original command instead

## Running tests

```bash
timeout 120 python -m pytest tests/ -v            # full suite (gate runs this)
timeout 60 python -m pytest tests/test_unit.py    # unit tests only
timeout 60 python -m pytest tests/test_queue.py   # specific file
timeout 60 python -m pytest tests/test_unit.py::TestTailLines  # specific class
```

## Architecture

- `switchboard/server/` — MCP server, tool definitions, HTTP handlers, ASGI app
- `switchboard/dispatch/` — Task execution engine: dispatch, worktree ops, gate pipeline, SDK integration
- `switchboard/db/` — SQLite async wrapper, schema migrations
- `switchboard/dashboard/` — REST API for the Switchboard SPA
- `switchboard/notifications/` — Slack and web push notifications
- `switchboard/git/` — Git operations, worktree management
- `switchboard/auth/` — OAuth middleware
- `tests/` — Pytest suite (unit, integration, smoke)

Root-level compat shims (`tasks.py`, `database.py`, `embedding_service.py`) remain for test compatibility.

## Deployment note

The `.claude/settings.json` in this repo contains a PreToolUse hook that blocks
`kill`/`pkill`/`killall` in CC workers. For the hook to apply to ALL CC workers
(not just those working on this repo), copy it to `~/.claude/settings.json` for
the worker user (`switchboard`) on the VPS:

```bash
cp .claude/settings.json /home/switchboard/.claude/settings.json
```
