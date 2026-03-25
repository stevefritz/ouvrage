# Switchboard — Developer Guide for CC Workers

## ⚠️ TWO DASHBOARD SYSTEMS — READ BEFORE TOUCHING FRONTEND

This repo has TWO dashboard systems. Always work in the NEW Foreman system.

**NEW Foreman (always use these):**
- `dashboard/foreman.html`, `dashboard/foreman-app.js`
- `dashboard/views/ProjectView.js`, `TaskView.js`, `ConversationView.js`, `LandingView.js`
- `dashboard/components/` — only components imported by the above views

**OLD Dashboard (never touch):**
- `dashboard/index.html`, `dashboard/app.js`
- `dashboard/components/ProjectDetail.js`, `TaskDetail.js`, `TaskPanel.js`

If your task says "dashboard" it means Foreman. If you're unsure, check the imports.

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

- `server.py` — MCP server, tool registration, HTTP handlers
- `tasks.py` — Task execution engine: dispatch, worktree ops, gate pipeline, SDK integration
- `database.py` — SQLite async wrapper, schema migrations
- `notifications.py` — Slack notifications
- `tests/` — Pytest suite (unit, integration, smoke)

## Deployment note

The `.claude/settings.json` in this repo contains a PreToolUse hook that blocks
`kill`/`pkill`/`killall` in CC workers. For the hook to apply to ALL CC workers
(not just those working on this repo), copy it to `~/.claude/settings.json` for
the worker user (`switchboard`) on the VPS:

```bash
cp .claude/settings.json /home/switchboard/.claude/settings.json
```
