# Analysis: Close vs Cancel Lifecycle Actions

## 1. Reference Map: `close` Action

### Lifecycle Transition Table (`lifecycle.py`)
| From State | Action | To State | Reason | Label |
|---|---|---|---|---|
| `stopped` | `close` | `completed` | `manually_closed` | "Close" |

Only one transition — `close` is only available from `stopped`.

### Side Effects (`lifecycle.py:66-92`)
1. **`_close_archive_and_cleanup`** (line 66): Archives task logs via `archive_task_logs(task, project, "close")`, then if `cleanup=True` (default), calls `cleanup_worktree(project, task, force_delete_branch)` and clears `worktree_path`. Also clears `gate_passed_at` and `held`.
2. **`_post_close_message`** (line 86): Posts status message "Manually closed — no gates or chain actions triggered."

### Preconditions (`lifecycle.py:842-900`)
- `_reject_if_working` (line 842): Rejects close if task is still running — "Cancel it first, then close."
- `_reject_if_awaiting_feedback_close` (line 896): Rejects if `reason == 'awaiting_feedback'` — "use Cancel Reopen instead"

### Engine Handler (`engine.py:929-942`)
```python
async def close_task(task_id, cleanup=True, force_delete_branch=False):
    # Delegates to lifecycle.execute(task_id, "close", cleanup=..., force_delete_branch=...)
    return {"task_id": task_id, "status": "completed", "cleaned_up": cleanup, "manually_closed": True}
```

### MCP Tool (`tools.py:345-363`)
- Listed in `transition_task` action enum: `["resume", "retry", "reopen", "start", "stop", "cancel", "close", "approve", "skip_gate"]`
- Options described: "close: cleanup (bool, default true), force_delete_branch (bool)"

### Dashboard API (`dashboard/api.py`)
- Endpoint: `/tasks/{id}/close` → `_handle_close()` (line 1093-1102)
- Extracts `cleanup` and `force_delete_branch` from JSON body
- Route suffix stripping includes `"/close"` (line 82-86)

### Frontend (`dashboard/views/TaskView.js`)
- Tooltip (line 439): "Destroy worktree and delete branch. Permanent."
- Confirm dialog (line 1852): "Close Task" / "Destroy worktree and delete branch? Cannot be undone."
- Action mapped to `api.closeTask(id)` in execution map

### Dashboard State Label (`lifecycle.py:1318`)
```python
("completed", "manually_closed"): {"label": "Closed", "color": "#10b981", "pulse": False}
```
Note: "Closed" displays as green (same as completed), not grey like cancelled.

### Frontend API (`dashboard/api.js:84`)
```javascript
closeTask: (id) => request(`/tasks/${eid(id)}/close`, { method: 'POST' })
```

### Tests
- `test_lifecycle.py:275-279` — Close from stopped → completed with reason=manually_closed
- `test_lifecycle.py:729-731` — State label test for completed/manually_closed → "Closed"
- `test_lifecycle.py:925-973` — Close with cleanup side effects (archive + worktree cleanup)
- `test_audit_log.py:364` — Audit log for close action

---

## 2. Reference Map: `cancel` Action

### Lifecycle Transition Table (`lifecycle.py`)
| From State | Action | To State | Reason | Label | Dashboard Visible? |
|---|---|---|---|---|---|
| `ready` | `cancel` | `cancelled` | — | "Cancel" | Yes |
| `working` | `cancel` | `cancelled` | — | *(no label)* | **No** — user must Stop first |
| `validating` | `cancel` | `cancelled` | — | *(no label)* | **No** — user must Stop first |
| `stopped` | `cancel` | `cancelled` | — | "Cancel" | Yes |

Also recovery-initiated:
| From State | Action | To State |
|---|---|---|
| `working` | `recover_cancel` | `cancelled` |
| `stopped` | `recover_cancel` | `cancelled` |

### Side Effects
**From ready:** `_revert_punchlist`, `_clear_held_flag`, `_drain_queue_effect`
**From working:** `_cancel_running_process`, `_revert_punchlist`, `_clear_held_flag`, `_drain_queue_effect`
**From validating:** Same as working
**From stopped:** `_revert_punchlist`, `_clear_held_flag`, `_drain_queue_effect`

### Preconditions
- `stopped → cancel`: `_reject_awaiting_feedback` — rejects if `reason == 'awaiting_feedback'`

### Engine Handler (`engine.py:838-846`)
```python
async def cancel_task(task_id):
    # Delegates to lifecycle.execute(task_id, "cancel")
    return {"task_id": task_id, "status": "cancelled"}
```
Also: `cancel_chain(task_id)` (line 889-912) — recursively cancels task + all dependents.

### Recovery from Cancelled (`lifecycle.py:1128-1140`)
| From State | Action | To State | Label |
|---|---|---|---|
| `cancelled` | `retry` | `working` | "Retry" |
| `cancelled` | `resume` | `working` | "Resume" (requires session_id) |

### MCP Tool
- Listed in `transition_task` action enum (same as close)

### Dashboard API (`dashboard/api.py`)
- `/tasks/{id}/cancel` → `_handle_cancel()` (line 996-998)
- `/tasks/{id}/cancel-chain` → `_handle_cancel_chain()` (line 1130-1132)
- `/tasks/{id}/cancel-reopen` → `_handle_cancel_reopen()` (line 1153-1155)

### Frontend
- Tooltip (line 434): "Kill the running CC process. Code changes preserved."
- Confirm dialog (line 1849): "Cancel Task" / "Kill the running CC process? Code changes preserved."
- Cancel Chain confirm (line 1861): "Cancel this task and all dependent tasks in the chain?"

### Status Label (`lifecycle.py:1320`)
```python
("cancelled", None): {"label": "Cancelled", "color": "#6b7280", "pulse": False}
```
Grey color (muted) — distinct from close which shows green.

### Frontend API (`dashboard/api.js`)
```javascript
cancelTask: (id) => request(`/tasks/${eid(id)}/cancel`, { method: 'POST' }),
cancelChain: (id) => request(`/tasks/${eid(id)}/cancel-chain`, { method: 'POST' }),
```

### `active_only` Filter (`db/tasks.py:216-218`)
```python
if active_only:
    conditions.append("t.status != 'cancelled'")
```
**Only `cancelled` is excluded.** Completed tasks (including manually_closed) are NOT excluded. This means closed tasks show up by default but cancelled don't.

### Constants (`config/constants.py`)
`CORE_STATE_DEFINITIONS` includes `"cancelled"` but NOT `"closed"` — closed is modeled as `completed` with `reason="manually_closed"`.

### Tests
- `test_lifecycle.py:214-228` — Cancel from ready/working/validating/stopped
- `test_lifecycle.py:287-295` — Retry & resume from cancelled
- `test_lifecycle_actions.py:177-191` — Resume from cancelled; retry from cancelled
- `test_audit_log.py:37-48, 134, 218` — Audit log entries for cancel action
- `test_crash_recovery.py:881` — recover_cancel action logged

---

## 3. Comparison: What Close Does That Cancel Doesn't

| Behavior | `cancel` | `close` |
|---|---|---|
| **Destination status** | `cancelled` | `completed` (with reason `manually_closed`) |
| **Dashboard display** | Grey "Cancelled" | Green "Closed" |
| **Archives logs** | No | Yes — calls `archive_task_logs()` |
| **Cleans up worktree** | No | Yes — `cleanup_worktree()` (if cleanup=True) |
| **Deletes remote branch** | No | Optional — `force_delete_branch` param |
| **Reverts punchlist** | Yes | No |
| **Kills running process** | Yes (from working/validating) | No (requires stopped first) |
| **Drains queue** | Yes | No |
| **Clears held flag** | Yes | Yes |
| **Posts status message** | No | Yes ("Manually closed") |
| **Excluded from `active_only`** | Yes — filtered out | No — shows with completed tasks |
| **Available from** | ready, working, validating, stopped | stopped only |
| **Has cleanup options** | No | Yes (cleanup, force_delete_branch) |
| **Confirmation required** | Yes | Yes |

### Key Insight
Close is essentially "cancel + cleanup + mark as completed." It archives logs, destroys the worktree, optionally deletes the branch, and puts the task in `completed` status. Cancel just marks it `cancelled` and leaves everything in place (worktree, branch, logs all preserved).

The fundamental confusion is that from `stopped`, both actions are available as dashboard buttons. From the user perspective:
- **Cancel** = "I don't want this task" (preserves everything)
- **Close** = "I don't want this task AND clean up after it" (destroys worktree/branch)

---

## 4. Design: Merge Close into Cancel

### Proposal

Merge `close` into `cancel` by:

1. **Add optional parameters to `cancel`**: `cleanup: bool = False`, `force_delete_branch: bool = False`
2. **Remove `close` from transition table** — delete `("stopped", "close")` entry
3. **Remove `close_task()` from engine.py** — all callers use `cancel_task()` with options
4. **Keep `cancelled` as the only terminal "unwanted" status** — remove `manually_closed` reason
5. **Add archive + cleanup side effects to cancel** when cleanup=True

### Changes Required

#### `switchboard/dispatch/lifecycle.py`
- Remove `("stopped", "close")` transition
- Remove `_close_archive_and_cleanup` and `_post_close_message` side effects (or repurpose)
- Add new conditional side effect to `("stopped", "cancel")` that checks `ctx.get("cleanup")` and runs archive + worktree cleanup if true
- Remove `("completed", "manually_closed")` from `STATE_LABELS`
- Remove `_reject_if_working` and `_reject_if_awaiting_feedback_close` preconditions (cancel already has its own guards)

#### `switchboard/dispatch/engine.py`
- Remove `close_task()` function
- Update `cancel_task()` signature: `cancel_task(task_id, cleanup=False, force_delete_branch=False)`
- Pass `cleanup` and `force_delete_branch` through to lifecycle.execute context

#### `switchboard/server/tools.py`
- Remove `"close"` from `transition_task` action enum
- Move cleanup/force_delete_branch options description to cancel

#### `switchboard/server/dispatch.py` and `switchboard/server/handlers/tasks.py`
- Remove close handler if separate; update transition_task handler to pass options to cancel

#### `switchboard/dashboard/api.py`
- Remove `/tasks/{id}/close` endpoint
- Update `/tasks/{id}/cancel` to accept optional `cleanup` and `force_delete_branch` body params
- Remove `"/close"` from suffix stripping list

#### `dashboard/api.js`
- Remove `closeTask` API method
- Update `cancelTask` to accept options: `cancelTask(id, { cleanup, force_delete_branch })`

#### `dashboard/views/TaskView.js`
- Remove close tooltip, confirm dialog
- Update cancel tooltip/confirm to mention optional cleanup
- Update action execution map
- Consider: add a "Cancel & Clean Up" button variant or a checkbox in the cancel confirm dialog

#### Tests
- Update all tests that reference close/closed/manually_closed
- Add tests for cancel with cleanup=True

### Migration Concerns
- **Audit log**: Historical `close` actions and `manually_closed` reasons in audit_log — no migration needed, they're just historical records
- **Existing tasks**: Any task currently in `completed` with `reason="manually_closed"` stays as-is. No DB migration needed — we just stop creating that combination.
- **`pr_status = 'closed'`**: This is unrelated — it refers to GitHub PR state, not task lifecycle. No changes needed.

---

## 5. Design: Add `delete_task`

### Overview
`delete_task(task_id)` permanently removes a task record and all associated data from the DB.

This is NOT a lifecycle action (not a state transition) — it's a separate handler/tool.

### Guardrails
- **Cannot delete working or validating tasks**: Must stop/cancel first. Check `status` before proceeding.
- **Confirmation**: Dashboard should require explicit confirmation with task ID visible.

### Cascade Deletion — Tables Referencing `task_id`

| Table | FK Column | ON DELETE CASCADE? | Action Needed |
|---|---|---|---|
| `task_checklist` | `task_id REFERENCES tasks(id)` | **No** | Explicit DELETE |
| `task_artifacts` | `task_id REFERENCES tasks(id)` | **No** | Explicit DELETE |
| `task_tags` | `task_id REFERENCES tasks(id)` | **No** | Explicit DELETE |
| `subtasks` | `task_id REFERENCES tasks(id)` | **No** | Explicit DELETE |
| `messages` | `task_id REFERENCES tasks(id)` | **No** | Explicit DELETE |
| `message_chunks` | `message_id REFERENCES messages(id)` | **Yes** | Auto-cascades when messages deleted |
| `files` | `task_id REFERENCES tasks(id)` | **No** | Explicit DELETE + disk file cleanup |
| `task_audit_log` | `task_id` (no FK constraint) | **No** | Explicit DELETE |
| `task_attempts` | `task_id REFERENCES tasks(id)` | **Yes** | Auto-cascades |
| `tasks_vec` | keyed by `tasks.rowid` | Trigger-based | Auto-fires via `tasks_vec_delete` trigger on tasks DELETE |
| `messages_vec` | keyed by `messages.id` | Trigger-based | Auto-fires via `messages_vec_delete` trigger on messages DELETE |
| `chunks_vec` | keyed by `message_chunks.id` | Trigger-based | Auto-fires via `chunks_vec_delete` trigger on message_chunks DELETE |

### Additional Cleanup (Non-DB)

| Resource | Action |
|---|---|
| **Worktree** | Call `cleanup_worktree(project, task, force_delete_branch=True)` if `worktree_path` exists on disk |
| **Remote branch** | Optional — `force_delete_branch=True` passed to cleanup_worktree handles this |
| **Log files** | Archived logs in `.switchboard/` dir of worktree — cleaned up with worktree |
| **Disk files** | `files` table has `path` column — delete physical files before DB records |

### Chain Task Handling
- Tasks with `depends_on` pointing to the deleted task: **Null out the `depends_on` field**. The dependent task becomes unblocked.
- Alternative: reject deletion if dependents exist (safer but more annoying). Recommend: null out with a warning in the response.

### Implementation

#### New DB function (`switchboard/db/tasks.py`)
```python
async def delete_task(task_id: str) -> dict:
    """Permanently delete a task and all associated records."""
    async with get_db() as db:
        # 1. Get task for validation
        task = await get_task(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")
        if task["status"] in ("working", "validating"):
            raise ValueError(f"Cannot delete {task['status']} task. Stop or cancel it first.")

        # 2. Get file paths for disk cleanup
        files = await db.execute_fetchall(
            "SELECT path FROM files WHERE task_id = ?", (task_id,)
        )

        # 3. Null out depends_on for dependent tasks
        await db.execute(
            "UPDATE tasks SET depends_on = NULL WHERE depends_on = ?", (task_id,)
        )

        # 4. Delete from tables without CASCADE (order matters for FK)
        await db.execute("DELETE FROM task_checklist WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM task_artifacts WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM subtasks WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM task_audit_log WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM files WHERE task_id = ?", (task_id,))
        # messages deletion cascades to message_chunks; vec triggers handle vec tables
        await db.execute("DELETE FROM messages WHERE task_id = ?", (task_id,))
        # task_attempts has ON DELETE CASCADE — auto-handled
        # tasks_vec trigger fires on task delete
        await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()

        return {"deleted": task_id, "files_cleaned": len(files)}
```

#### New engine function (`switchboard/dispatch/engine.py`)
```python
async def delete_task(task_id: str, force_delete_branch: bool = True) -> dict:
    """Permanently delete a task, cleaning up worktree and DB records."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    # Cleanup worktree + branch if exists
    project = await db.get_project(task["project_id"])
    if project and task.get("worktree_path"):
        await cleanup_worktree(project, task, force_delete_branch)

    # Delete disk files
    files = await db.get_task_files(task_id)  # or query directly
    for f in files:
        path = f.get("path")
        if path and os.path.exists(path):
            os.remove(path)

    # Delete all DB records
    return await db.delete_task(task_id)
```

#### New MCP tool (`switchboard/server/tools.py`)
```python
Tool(
    name="delete_task",
    description="Permanently delete a task and all associated data. Cannot delete working/validating tasks.",
    inputSchema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "force_delete_branch": {"type": "boolean", "default": True},
        },
        "required": ["task_id"],
    },
)
```

#### Dashboard API endpoint (`switchboard/dashboard/api.py`)
```
DELETE /dashboard/api/tasks/{id}  →  delete_task(task_id)
```

#### Dashboard UI
- Add "Delete" button to cancelled and completed tasks
- Confirmation dialog: "Permanently delete this task? All data (messages, checklist, files, logs) will be destroyed. This cannot be undone."

---

## 6. Dashboard Button Changes

### Current Buttons (from transition table labels)
| State | Current Buttons |
|---|---|
| ready | Dispatch, Cancel, Approve (if held) |
| working | Stop |
| validating | Stop, Skip Gate |
| stopped | Resume, Retry, Start (if awaiting_feedback), Cancel, Close, Skip Gate (if gate failed), Cancel Reopen (if awaiting_feedback) |
| completed | Reopen |
| cancelled | Retry, Resume |

### Proposed Buttons (after merge + delete)
| State | Proposed Buttons |
|---|---|
| ready | Dispatch, Cancel, Approve (if held) |
| working | Stop |
| validating | Stop, Skip Gate |
| stopped | Resume, Retry, Start (if awaiting_feedback), Cancel, Skip Gate (if gate failed), Cancel Reopen (if awaiting_feedback) |
| completed | Reopen, **Delete** |
| cancelled | Retry, Resume, **Delete** |

### Notes
- **Close removed** from stopped — cancel with optional cleanup replaces it
- **Delete added** to completed and cancelled as a new action (not a lifecycle transition, separate button)
- Cancel's confirm dialog should offer cleanup option: "Cancel this task?" with a "Clean up worktree and branch" checkbox
- Delete should have its own confirm with strong warning about permanent data loss

---

## 7. Risks and Edge Cases

1. **Historical audit entries**: `close` action and `manually_closed` reason exist in `task_audit_log`. These are just history — no code reads them to make decisions. Safe to leave as-is.

2. **Existing `completed`+`manually_closed` tasks**: About 10-20 may exist. The `STATE_LABELS` entry `("completed", "manually_closed")` should be kept for backward compat display, even after removing the ability to create new ones. Eventually can be removed once old tasks are deleted.

3. **`active_only` filter asymmetry**: Currently only `cancelled` is excluded by `active_only`. Closed tasks (completed+manually_closed) show up. After merge, all unwanted tasks go to `cancelled` and get filtered — cleaner behavior.

4. **Chain invalidation**: `engine.py:207-236` auto-cancels downstream tasks during chain invalidation. This uses `cancel_task()` which goes to `cancelled`. No impact from removing close.

5. **PR status `closed`**: This refers to GitHub PR state, not task lifecycle. Completely unrelated. No changes needed. Appears in `pr_sweep.py` and `db/tasks.py:get_tasks_with_open_prs()`.

6. **`cancel_reopen`**: This is a separate action `("stopped", "cancel_reopen") → completed`. Not affected by this merge — it's about reverting a reopen, not cancelling the task.

7. **Delete of task with dependents**: If task B `depends_on` task A, and A is deleted, B's `depends_on` should be nulled out so B doesn't become permanently blocked. The delete handler should handle this.

8. **SQLite FK enforcement**: SQLite FKs are enforced (PRAGMA foreign_keys = ON in connection.py). Most FK constraints on task_id do NOT have ON DELETE CASCADE, so explicit DELETEs are required before deleting the task row itself.

9. **Vec table triggers**: The `tasks_vec_delete` trigger automatically fires on task deletion. The `messages_vec_delete` and `chunks_vec_delete` triggers fire when messages/chunks are deleted. Order matters: delete messages before task.

10. **File cleanup race**: If a worker is writing files when delete is called, there could be a race. The status guard (reject working/validating) prevents this.
