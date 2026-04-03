# Queued Task Reason Reference

This document catalogs every possible queued reason a task can have, how each is determined, what the user sees on the TaskView page, and the rationale behind each.

---

## Overview

When a task has status **QUEUED** (i.e., `queued_at` is set in the DB), the system determines *why* it is queued. The reason is computed by `_determine_queued_reason()` in `switchboard/dispatch/lifecycle.py` and returned as part of the `/dashboard/api/tasks/:id/actions` response under `state.queued_reason`.

Priority order (first match wins):

1. **dependency** — `depends_on` is set and the parent task has not yet gate-passed
2. **project_paused** — the task's project has `paused = TRUE`
3. **component_paused** — the task's component has `paused = TRUE`
4. **concurrency** — fallback: no other blocking condition; all dispatch slots are occupied

---

## Reason: `dependency`

| Field | Value |
|-------|-------|
| Icon | 🔗 |
| Display text | `Waiting on: {shortId}` |
| Interaction | `{shortId}` is a tappable link navigating to the blocking task's TaskView |

### How it is determined

1. The task has a `depends_on` field pointing to a parent task ID.
2. The parent task does **not** have `gate_passed_at` set (i.e., it hasn't finished and passed gates).

**Code path:** `_determine_queued_reason` reads the parent task from DB and checks `gate_passed_at IS NULL`.

### Why this matters

Dependent tasks are dispatched automatically by `_check_and_dispatch_dependents()` once the parent task's gates pass. Until that happens, even if a concurrency slot is available, the task should not dispatch — it would have no branch to build on or logic to depend on. The FIFO drain queue (`get_queued_tasks`) explicitly excludes these tasks.

### When it resolves

When the parent task completes and its test + review gates pass (`gate_passed_at` is set), `_check_and_dispatch_dependents` triggers a queue drain, which re-evaluates this task and dispatches it.

---

## Reason: `project_paused`

| Field | Value |
|-------|-------|
| Icon | ⏸ |
| Display text | `Project paused` |
| Interaction | None (plain text) |

### How it is determined

The task's project row has `paused = 1` in the DB. Checked via `db.get_project(task["project_id"])`.

### Why this matters

Project pause is a deliberate operator action to halt all dispatch for a project (e.g., during incidents, deployments, or freeze windows). Any task that tries to dispatch while the project is paused is blocked at the `dispatch_task()` entry point. If the task was already in the queue when the project was paused, it will remain queued until resumed.

### When it resolves

When `resume_project(project_id)` is called, which sets `paused = FALSE`. The queue drain runs on each dispatch attempt and will pick up the task once the project is unpaused.

---

## Reason: `component_paused`

| Field | Value |
|-------|-------|
| Icon | ⏸ |
| Display text | `Component paused` |
| Interaction | None (plain text) |

### How it is determined

The task has a `component_id` and that component row has `paused = 1` in the DB. Checked via `db.get_component(task["component_id"])`.

### Why this matters

Component pause is a finer-grained version of project pause — it affects only tasks assigned to a specific component, leaving other components in the project unaffected. Useful when one feature area is frozen while work continues elsewhere.

**Note:** `paused` must be in `COMPONENT_MUTABLE_FIELDS` (in `switchboard/config/constants.py`) for `update_component(paused=True/False)` to persist the value. This was added as part of the queued-task-reason feature.

### When it resolves

When `resume_component(component_id)` is called, which sets `paused = FALSE`.

---

## Reason: `concurrency`

| Field | Value |
|-------|-------|
| Icon | ⏳ |
| Display text | `Waiting for concurrency slot` |
| Interaction | None (plain text) |

### How it is determined

None of the above conditions apply. The task is in the FIFO queue and waiting for an active task slot to open up. The concurrency limit is determined by `db.get_concurrency_limit()`, which reads the DB-stored value (set on the instance config) or falls back to `DEFAULT_MAX_CONCURRENT`.

**Code path:** `queue.py` `_drain_queue()` checks `count_active_tasks() >= get_concurrency_limit()`. If slots are full, no task is dispatched even if tasks are in the queue.

### Why this matters

The system limits the number of simultaneously running CC sessions to prevent resource exhaustion on the host VPS. When the limit is reached, new tasks are queued FIFO (by `queued_at` timestamp, with recovery tasks prioritized via `recovery_priority`).

### When it resolves

When any running task finishes (completes, fails, cancels, or is stopped), `_drain_queue()` is called as a side effect of the status transition. It checks whether a slot is now available and dispatches the oldest eligible queued task.

---

## Data Flow

```
task.queued_at IS NOT NULL
        │
        ▼
_determine_queued_reason(task)
        │
        ├── depends_on set AND parent.gate_passed_at IS NULL  →  ("dependency", parent_id)
        ├── project.paused = TRUE                             →  ("project_paused", None)
        ├── component.paused = TRUE                           →  ("component_paused", None)
        └── (fallback)                                        →  ("concurrency", None)
        │
        ▼
lifecycle.get_state_label(task_id)
  returns: { ..., queued_reason, queued_blocking_task_id }
        │
        ▼
GET /dashboard/api/tasks/:id/actions
  response.state.queued_reason
  response.state.queued_blocking_task_id
        │
        ▼
TaskView.js StatusLine
  taskState.queued_reason → reason line rendered below status badge
```

---

## UI Rendering

The reason line appears in `StatusLine` in `dashboard/views/TaskView.js` only when `taskState?.reason === 'queued'` and `taskState?.queued_reason` is set.

Styling uses design tokens from `dashboard/tokens.js`:
- `colors.textTertiary` — muted text color (`#8a93a2`)
- `typography.size.sm` — 13px font size
- `typography.fontBody` — body font (not monospace)
- `paddingLeft: '20px'` — aligns with status label text

The dependency reason renders `{shortId}` as a clickable `<a>` link navigating to the blocking task's TaskView via `routes.task(blockingTaskId)`. Link uses dotted underline to indicate navigability without heavy styling.
