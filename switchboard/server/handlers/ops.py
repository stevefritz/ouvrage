"""Ops tool handlers — get_context and get_guide."""

import switchboard.db as db

GUIDE_STATIC = """# Ouvrage Guide

## What is Ouvrage?

Ouvrage is an async task orchestration system for Claude Code sessions. Think of it as a **PM/tech lead layer** that dispatches work to autonomous CC agents, monitors their progress, and manages a quality gate pipeline (test → review → PR).

### Mental Model
- **You** (PM/tech lead) define specs, create tasks, and monitor progress
- **CC workers** execute tasks in isolated git worktrees with full autonomy
- **Gate pipeline** automatically runs tests, dispatches reviews, and creates PRs

## Available Tools by Workflow

### Planning & Setup
| Tool | Purpose |
|---|---|
| `create_project` | Register a repo with working dir, setup commands, test commands |
| `create_conversation` | Start a design conversation (specs, plans, Q&A) |

### Dispatching Work
| Tool | Purpose |
|---|---|
| `dispatch_task` | Create a task and launch a CC session (non-blocking) |
| `resume_task` | Resume a paused task with the same session (preserves context) |
| `retry_task` | Start a fresh session (injects review feedback if posted) |

### Monitoring
| Tool | Purpose |
|---|---|
| `get_task_status` | Full task status: checklist, messages, artifacts, liveness |
| `list_tasks` | List tasks with filters (project, status, tag) |
| `get_session_log` | CC's tool calls and text output (JSONL) |
| `get_dispatch_log` | Dispatch metadata, cost, timing |
| `get_pipeline` | View the full dependency chain for a task |

### Communication
| Tool | Purpose |
|---|---|
| `post_task_message` | Post to a task's message thread |
| `read_task_messages` | Read messages (cursor-based polling) |
| `search_task_messages` | Full-text search across all task messages |

### Conversations (async message board)
| Tool | Purpose |
|---|---|
| `board` | Dashboard of active conversations |
| `post` | Post to a conversation |
| `read` | Read messages (cursor-based) |
| `get_pinned` | Get the source-of-truth pinned message |

### Bulk Operations
| Tool | Purpose |
|---|---|
| `update_task` | Update any mutable task field (model, retry_after, gates, etc.) |
| `bulk_update_tasks` | Update multiple tasks at once |

### Control (Pause/Stop/Resume)
| Tool | Purpose |
|---|---|
| `pause_project` | Pause entire project |
| `stop_project` | Pause + cancel all running tasks in project |
| `resume_project` | Resume a paused project |

## Pipeline Features

### Auto-Merge
Set `auto_merge=true` on dispatch. When gate passes: merge branch into target → auto-release worktree → advance chain. Chain-aware: child merges into parent branch, falls back to main when parent already merged.

### Crash Recovery
Three-layer self-healing:
1. **Graceful shutdown** — marks working tasks for recovery before service stops
2. **Signal detection** — SIGTERM/SIGKILL keeps tasks as "working" not "failed"
3. **Health check** (every 60s) — finds dead PIDs, orphaned tasks, stalled chains, rate-limited tasks past retry time

### Rate Limiting
When CC hits usage limits, the task is parked as `rate-limited` with a `retry_after` timestamp parsed from the error message. The health check auto-dispatches it when limits reset. You can also set `retry_after` manually on any task for custom backoff.

## Common Patterns

1. **Starting a feature**: `dispatch_task` with `depends_on` to chain tasks
2. **Task chains**: Use `depends_on` to create sequential pipelines — next task auto-dispatches when gate passes
3. **Review workflow**: Post feedback with `post_task_message(type='review')`, then `retry_task` — feedback is auto-injected
4. **Resuming work**: Use `resume_task` to continue with the same session context (preserves CC's memory)
5. **Config inheritance**: Project → Task. Set `model`, `auto_test`, etc. at project or task level
6. **Auto-merge chains**: Set `auto_merge=true` on all tasks in a chain — they merge sequentially as each passes
7. **Delayed dispatch**: Set `retry_after` on a task to schedule it for a specific time
8. **Kill switch**: `stop_project` to immediately halt all work

## Anti-Patterns

- **Don't write the implementation plan** — CC does that during its grounding phase
- **Don't micromanage** — give clear specs and let CC work autonomously
- **Don't use retry when you mean resume** — retry clears the session and starts fresh; resume continues
- **Don't skip the spec** — tasks without specs produce worse results
- **Don't set auto_test=false** unless you have a good reason — the gate catches most issues
- **Don't use pkill/kill in CC sessions** — CC runs in a process group and will terminate itself
- **Don't set auto_merge and auto_pr on the same task** — they're mutually exclusive
"""


async def _handle_get_context(arguments):
    """Lightweight orientation snapshot — call first in every conversation."""
    projects = await db.list_projects()
    task_counts = await db.get_project_task_counts()
    active_count = await db.count_active_tasks()

    # Project summaries
    project_lines = []
    for p in projects:
        counts = task_counts.get(p["id"], {})
        total = counts.get("total_tasks", 0)
        active = counts.get("active_task_count", 0)
        cost = counts.get("total_cost", 0)
        project_lines.append(f"  - {p['id']}: {total} tasks ({active} active), ${cost:.2f}")

    # Active/blocked tasks
    active_tasks = await db.list_tasks(status="working")
    blocked_tasks = await db.list_tasks(status="needs-review")
    rate_limited = await db.list_tasks(status="rate-limited")

    task_lines = []
    for t in (active_tasks or [])[:5]:
        phase = f" [{t.get('phase', '')}]" if t.get("phase") else ""
        task_lines.append(f"  - {t['id']}{phase} — {(t.get('goal') or '')[:60]}")
    for t in (blocked_tasks or [])[:3]:
        task_lines.append(f"  - {t['id']} [needs-review] — {(t.get('goal') or '')[:60]}")
    for t in (rate_limited or [])[:3]:
        task_lines.append(f"  - {t['id']} [rate-limited] — {(t.get('goal') or '')[:60]}")

    # Recent significant events
    events = await db.get_recent_activity(limit=5)
    event_lines = []
    for ev in events:
        task_short = ev.get("task_id", "").split("/")[-1] if ev.get("task_id") else ""
        title = ev.get("title") or ev.get("event_type", "")
        event_lines.append(f"  - [{ev.get('created_at', '')[:16]}] {task_short}: {title}")

    # Pinned conversations
    convs = await db.list_conversations()
    pinned_convs = [c for c in convs if c.get("has_pinned")]

    parts = [
        f"# Ouvrage Context",
        f"",
        f"**Projects:** {len(projects)} | **Active tasks:** {active_count}",
        f"",
    ]

    if project_lines:
        parts.append("## Projects")
        parts.extend(project_lines)
        parts.append("")

    if task_lines:
        parts.append("## Active / Attention Needed")
        parts.extend(task_lines)
        parts.append("")

    if event_lines:
        parts.append("## Recent Events")
        parts.extend(event_lines)
        parts.append("")

    if pinned_convs:
        parts.append("## Conversations with Pinned Context")
        for c in pinned_convs[:10]:
            parts.append(f"  - `{c['id']}`: {c.get('goal', '')[:80]}")
        parts.append("")

    parts.append("_Call `get_guide` for the full tool reference. Use `conversations(search=...)` to find prior context._")

    return {"context": "\n".join(parts)}


async def _handle_get_guide(arguments):
    """Return the Switchboard guide with live system summary appended."""
    parts = [GUIDE_STATIC]

    # Live system summary
    projects = await db.list_projects()
    task_counts = await db.get_project_task_counts()
    active_count = await db.count_active_tasks()

    # Count components
    component_count = 0
    for p in projects:
        components = await db.list_components(project_id=p["id"])
        component_count += len(components)

    parts.append("## Live System Summary\n")
    parts.append(f"- **Projects**: {len(projects)}")
    parts.append(f"- **Active tasks**: {active_count}")
    parts.append(f"- **Components**: {component_count}")
    parts.append("")

    if projects:
        parts.append("### Projects")
        for p in projects:
            counts = task_counts.get(p["id"], {})
            total = counts.get("total_tasks", 0)
            active = counts.get("active_task_count", 0)
            cost = counts.get("total_cost", 0)
            parts.append(f"- **{p['id']}**: {total} tasks ({active} active), ${cost:.2f} total cost")

    return {"guide": "\n".join(parts)}
