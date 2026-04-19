# Ouvrage Dashboard — Implementation Spec

## Overview

A read-heavy SPA served from the existing Ouvrage server (bare metal, port 8100). Provides real-time visibility into task execution with limited action capabilities (cancel, retry, resume). No build step — vanilla JS, Tailwind via CDN, dark theme.

## Architecture

```
Browser → Caddy (basic auth) → Ouvrage :8100
                                  ├── /mcp          (MCP Streamable HTTP — existing)
                                  ├── /dashboard     (static SPA files)
                                  └── /dashboard/api (JSON REST endpoints)
```

- **Auth:** Caddy basic auth covers all `/dashboard*` routes. Dashboard API paths added to `UNPROTECTED_PATHS` in `auth.py` (OAuth not needed — Caddy handles it, and these endpoints are not MCP).
- **Serving:** Ouvrage mounts a Starlette `StaticFiles` for `/dashboard` and API routes alongside the existing MCP app.
- **Data:** All endpoints read from the same SQLite DB via `database.py`.
- **Real-time:** Polling at 10s intervals (v1). SSE upgrade path exists since we already have the ASGI infrastructure.

## Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| CSS | Tailwind CDN | Dark theme, utility classes, no build |
| JS | Vanilla ES modules | No framework overhead, <500 lines total |
| Markdown | marked.js CDN | Render message content (specs, reviews) |
| Server | Starlette routes | Already in the ASGI app |
| Data | SQLite via database.py | Already exists |

## File Structure

```
mcp-switchboard/
  dashboard/
    index.html          # SPA shell — nav, view containers, templates
    app.js              # Router, view rendering, polling, actions
    api.js              # Fetch wrapper for all API calls
    style.css           # Minimal custom styles (scrollbars, animations)
  dashboard_api.py      # All REST endpoints — mounted in server.py
```

4 files total. The HTML contains `<template>` elements for each component. JS clones and populates them.

## API Endpoints

All under `/dashboard/api/`. All return JSON.

### Read Endpoints

```
GET /tasks
  Query params: ?status=working&project_id=ym-discount-engine
  Returns: [{id, project_id, goal, status, phase, branch, checklist_total,
             checklist_done, total_cost_usd, last_activity, dispatch_count}]
  Sort: working first, then by last_activity desc

GET /tasks/{task_id}
  Returns: Full task object + checklist + recent_messages + artifacts
  Same shape as get_task_status MCP tool but without log_tail

GET /tasks/{task_id}/messages
  Query params: ?limit=50&offset=0
  Returns: [{id, author, type, title, content, pinned, created_at}]
  Sort: created_at asc

GET /tasks/{task_id}/session-log
  Returns: Parsed JSONL from .ouvrage/session.jsonl
  [{timestamp, type, content_preview, tool_name, stop_reason, cost_usd, num_turns}]
  Returns [] if no log file exists

GET /tasks/{task_id}/dispatch-log
  Returns: Raw text of .ouvrage/dispatch.log

GET /projects
  Returns: [{id, repo, default_branch, working_dir, active_task_count, total_cost}]

GET /projects/{project_id}
  Returns: Full project + all tasks for that project

GET /system
  Returns: {active_tasks, max_concurrent, uptime_seconds, version}
```

### Action Endpoints

```
POST /tasks/{task_id}/cancel
  Returns: {task_id, status: "cancelled"}

POST /tasks/{task_id}/retry
  Body: {clean: false}  (optional)
  Returns: dispatch result object

POST /tasks/{task_id}/resume
  Returns: dispatch result object
```

## Views

### 1. Board View (`/dashboard` or `/dashboard#/`)

The main landing page. A filterable task table.

**Layout:**
```
┌─────────────────────────────────────────────────────────────────────┐
│  OUVRAGE                              [2 active] [$4.23 total]  │
├─────────────────────────────────────────────────────────────────────┤
│  Filters: [All statuses ▾]  [All projects ▾]          Auto-refresh ●│
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ● WORKING   ym-discount-engine/review-marketing-provider           │
│              Code review of feature/marketing-provider-system...     │
│              Phase: analysis — Reading changed files                 │
│              ▓▓▓░░░░░░ 3/9    $0.00    2m ago              [Cancel] │
│                                                                     │
│  ● COMPLETED mcp-switchboard/write-claude-md                        │
│              Write CLAUDE.md for the Ouvrage repo               │
│              ▓▓▓▓▓▓▓▓▓ 9/9    $1.25    47m ago                     │
│                                                                     │
│  ✕ FAILED    ym-discount-engine/review-marketing-provider (prev)    │
│              Control request timeout: initialize                     │
│              0/9    $0.00    32m ago                        [Retry]  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Table columns:**
| Column | Content |
|---|---|
| Status | Colored badge with icon |
| Task | `project/task-id` linked to detail view |
| Goal | Truncated to ~60 chars |
| Phase | Current phase + detail text |
| Progress | Mini progress bar + `done/total` |
| Cost | `$X.XX` |
| Activity | Relative time ("2m ago") |
| Actions | Context-dependent buttons |

**Action buttons by status:**
| Status | Actions |
|---|---|
| working | Cancel |
| failed | Retry |
| needs-review | Resume |
| completed | (none) |
| cancelled | Retry |
| ready | (none) |

**Behavior:**
- Auto-polls `GET /tasks` every 10 seconds
- Clicking a row navigates to task detail
- Status filter persists in URL hash
- Working tasks sort to top, then by last_activity desc

### 2. Task Detail View (`/dashboard#/tasks/{id}`)

**Header:**
```
┌─────────────────────────────────────────────────────────────────────┐
│  ← Back to board                                                    │
│                                                                     │
│  ● WORKING   ym-discount-engine/review-marketing-provider           │
│  Code review of feature/marketing-provider-system against SUZY-1324 │
│                                                                     │
│  Branch: review-marketing-provider    Dispatches: 2                 │
│  Worktree: /work/ym-discount-engine/review-marketing-provider       │
│  Cost: $0.58    Tokens: 334K in / 4.3K out                         │
│                                                        [Cancel]     │
└─────────────────────────────────────────────────────────────────────┘
```

**Checklist panel:**
```
┌─ Checklist (3/9) ───────────────────────────────────────────────────┐
│  ✅ Read all new/changed files on feature branch                    │
│  ✅ Understand provider abstraction architecture                    │
│  ✅ Review Attentive provider implementation                        │
│  ⬜ Review Klaviyo provider implementation                          │
│  ⬜ Review database migrations                                      │
│  ⬜ Review artisan commands                                          │
│  ⬜ Review queue jobs and rollback logic                             │
│  ⬜ Compare against SUZY-1324 requirements                          │
│  ⬜ Post analysis/review via post_task_message                      │
└─────────────────────────────────────────────────────────────────────┘
```

**Messages panel:**
```
┌─ Messages ──────────────────────────────────────────────────────────┐
│                                                                     │
│  📌 SPEC — dispatcher — 14:18:12                                    │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ ## Code Review: Marketing Provider System (SUZY-1324)        │   │
│  │ **Branch:** feature/marketing-provider-system                │   │
│  │ ...                                          [Expand ▾]      │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  STATUS — dispatcher — 14:19:14                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Dispatch error: Control request timeout: initialize          │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  PROGRESS — cc-worker — 14:25:34                                    │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Fetched branch. Reading through changed files...             │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  RESULT — cc-worker — 14:32:10                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ # Marketing Provider System — Code Review                    │   │
│  │ ## 1. What Was Built                                         │   │
│  │ ...rendered markdown...                      [Expand ▾]      │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Message styling by type:**
| Type | Color accent | Icon |
|---|---|---|
| spec | blue-500 left border | 📌 (pinned) |
| progress | green-500 left border | — |
| question | amber-500 left border | ❓ |
| status | slate-500 left border | — |
| result | purple-500 left border | — |

**Message content rendering:**
- Render markdown via marked.js (specs and results will have headers, code blocks, lists)
- Long messages collapsed by default (>300px height), click to expand
- Pinned messages always show at top

**Session Log panel (collapsible, default closed):**
```
┌─ Session Log [▸ Expand] ───────────────────────────────────────────┐
└─────────────────────────────────────────────────────────────────────┘

Expanded:
┌─ Session Log ──────────────── [Text ▾] [Tools ▾] [All ▾] ─────────┐
│                                                                     │
│  14:24:15  SYSTEM  init                                             │
│  14:24:16  TEXT    Let me fetch the branch and examine the changes. │
│  14:24:16  TOOL    Bash → git fetch origin feature/marketing-...    │
│  14:24:17  RESULT  (success)                                        │
│  14:24:17  TEXT    Now let me look at the diff stats...             │
│  14:24:18  TOOL    Bash → git diff origin/suzy-discount-engine...   │
│  14:24:19  RESULT  (234 bytes)                                      │
│  14:24:20  TEXT    I can see 100 files changed. Let me read the...  │
│  14:24:20  TOOL    Read → app/Contracts/MarketingCodeProviderCon... │
│  ...                                                                │
│                                                                     │
│  14:31:45  TOOL    mcp__ouvrage__post_task_message → ...        │
│  14:31:46  RESULT  (success)                                        │
│  14:31:46  DONE    19 turns | 103s | $0.59                          │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Session log row types:**
| JSONL type | Display |
|---|---|
| SystemMessage | Gray, italic, shows subtype |
| AssistantMessage (text block) | White, shows truncated text |
| AssistantMessage (tool_use block) | Cyan, shows tool name + truncated input |
| UserMessage (tool_result) | Dim, shows tool_use_id + preview length |
| ResultMessage | Green/red, shows turns + duration + cost |

**Dispatch Log panel (collapsible, default closed):**
Raw preformatted text from `dispatch.log`. Useful for debugging.

**Auto-refresh:** Poll task detail every 5 seconds while status is `working`.

### 3. Projects View (`/dashboard#/projects`)

Simple table:
```
┌─────────────────────────────────────────────────────────────────────┐
│  PROJECTS                                                           │
├─────────────────────────────────────────────────────────────────────┤
│  ym-discount-engine                                                 │
│  StephenBadger/ym-discount-engine  branch: suzy-discount-engine     │
│  1 active task    $1.25 total                                       │
│                                                                     │
│  mcp-switchboard                                                    │
│  Blue-Badger-Team/mcp-switchboard  branch: main                     │
│  0 active tasks   $1.25 total                                       │
└─────────────────────────────────────────────────────────────────────┘
```

Click navigates to filtered board view (`#/?project_id=ym-discount-engine`).

## Design System

**Colors (Tailwind classes):**
```
Background:     bg-slate-950 (page), bg-slate-900 (cards), bg-slate-800 (inputs/hover)
Borders:        border-slate-700
Primary text:   text-slate-100
Secondary text: text-slate-400
Muted text:     text-slate-500

Status colors:
  working:      bg-emerald-500/20 text-emerald-400  (pulsing dot)
  completed:    bg-blue-500/20 text-blue-400
  failed:       bg-red-500/20 text-red-400
  needs-review: bg-amber-500/20 text-amber-400
  cancelled:    bg-slate-500/20 text-slate-400
  ready:        bg-slate-500/20 text-slate-300
```

**Typography:**
- Body: system sans-serif (Tailwind default)
- Code/paths/IDs: `font-mono text-sm`
- Message content: rendered markdown with prose styling

**Spacing:** Consistent `p-4` cards, `gap-4` between sections, `p-6` page padding.

**Responsive:** Single column on mobile, full table on desktop. Not a priority but Tailwind makes it cheap.

## Server-Side Changes

### New: `dashboard_api.py`
- All REST endpoint handlers
- Uses `database.py` for reads, `tasks.py` for actions
- Reads session JSONL files directly from worktree paths
- Starlette route handlers (not MCP tools)

### Modified: `server.py`
- Import and mount `dashboard_api` routes under `/dashboard/api`
- Mount `StaticFiles(directory="dashboard")` at `/dashboard`
- Ensure `/dashboard/` serves `index.html` (SPA fallback)

### Modified: `auth.py`
- Add `/dashboard` prefix to unprotected paths (Caddy handles auth externally)

### Modified: Caddyfile
- Add basic auth block for `ouvrage.example.dev/dashboard*`

## Implementation Order

1. **`dashboard_api.py`** — all endpoints, test with curl
2. **Server mounting** — static files + API routes in `server.py`, auth bypass
3. **`index.html`** — SPA shell, nav, Tailwind, marked.js CDN
4. **`api.js`** — fetch wrapper
5. **`app.js`** — router + board view (task list with polling)
6. **Task detail view** — checklist + messages with markdown rendering
7. **Session log viewer** — JSONL parser and timeline
8. **Action buttons** — cancel/retry/resume with confirmation
9. **Projects view** — simple, last priority
10. **Caddy config** — basic auth on `/dashboard*`
11. **Deploy** — copy to `/opt/ouvrage/`, restart

## Not In Scope (v1)

- SSE / WebSocket push (polling is fine)
- Editing projects or tasks from the dashboard
- User authentication beyond Caddy basic auth
- Historical analytics or charts
- Log streaming (live tail of CC output)
- Mobile-optimized layout
- Search across tasks/messages
