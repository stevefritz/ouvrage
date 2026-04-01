# Lifecycle System Consolidation Audit

**Date:** 2026-04-01
**Scope:** Full audit of the task lifecycle system as-built after the 3-phase refactor (~15 tasks, ~$60 total cost).
**Purpose:** This document replaces the original design report (`lifecycle-state-machine-design.md`) as the source of truth for the lifecycle system.

---

## Part 1: System As-Built

### 1.1 Architecture Overview

The lifecycle system lives in `switchboard/dispatch/lifecycle.py` (1,468 lines). It consists of:

- **`TaskLifecycle` class** — singleton (`lifecycle`) with three public methods:
  - `execute(task_id, action, **context)` — the single entry point for all state transitions
  - `get_available_actions(task_id)` — returns valid user-facing actions for dashboard buttons
  - `get_state_label(task_id)` — returns display label, color, and pulse for dashboard
- **`TRANSITIONS` dict** — declarative transition table mapping `(effective_state, action) → TransitionDef`
- **`TransitionDef` dataclass** — defines target state, reason, preconditions, side effects, button label/style
- **`_effective_state()` method** — maps old DB status values to the 6-state model
- **`STATE_LABELS` dict** — maps `(state, reason)` to user-facing display info
- **Side-effect functions** — ~30 async functions that run after state transitions

### 1.2 The 6-State Model

| State | DB values that map to it | Description |
|-------|--------------------------|-------------|
| `ready` | `ready`, `blocked` | Task created, not yet dispatched |
| `working` | `working` | CC session active |
| `validating` | `pending-validation`, `turns-exhausted` (with active gates) | Gate pipeline running |
| `stopped` | `stopped`, `needs-review`, `turns-exhausted` (no gates), `rate-limited`, `failed`, `reopened` | Halted, awaiting action |
| `completed` | `completed`, `merged` | Done (gates passed, manually closed, or skipped) |
| `cancelled` | `cancelled` | Cancelled by user or system |

The `reason` field carries context within each state (e.g., `stopped/paused_by_user` vs `stopped/recovery_failed`).

### 1.3 Complete Transition Table

36 transitions total. Format: `(from_state, action) → to_state [reason]`

#### User-Initiated Actions (shown as dashboard buttons)

| # | From | Action | To | Reason | Preconditions | Side Effects | Button Label | Style |
|---|------|--------|----|--------|---------------|-------------|-------------|-------|
| 1 | ready | dispatch | working | — | — | `_dispatch_launch_session` | Dispatch | primary |
| 2 | ready | cancel | cancelled | — | — | `_revert_punchlist`, `_clear_held_flag`, `_drain_queue_effect` | Cancel | danger |
| 3 | working | stop | stopped | paused_by_user | — | `_stop_cc_session`, `_post_stop_message`, `_drain_queue_effect` | Stop | secondary |
| 4 | working | cancel | cancelled | — | — | `_cancel_running_process`, `_revert_punchlist`, `_clear_held_flag`, `_drain_queue_effect` | *(no label — hidden)* | danger |
| 5 | validating | stop | stopped | paused_by_user | — | `_stop_gate_subprocess`, `_post_stop_message`, `_drain_queue_effect` | Stop | secondary |
| 6 | validating | skip_gate | completed | gate_skipped | — | `_skip_gate_set_fields`, `_skip_gate_post_message`, `_skip_gate_dispatch_dependents` | Skip Gate | secondary |
| 7 | validating | cancel | cancelled | — | — | `_cancel_running_process`, `_revert_punchlist`, `_clear_held_flag`, `_drain_queue_effect` | *(no label — hidden)* | danger |
| 8 | stopped | resume | working | — | `_require_session_or_gate_resumable` | `_resume_launch_session` | Resume | primary |
| 9 | stopped | retry | working | — | — | `_retry_launch_session` | Retry | primary |
| 10 | stopped | start | working | — | `_require_awaiting_feedback` | `_start_launch_session` | Start | primary |
| 11 | stopped | skip_gate | completed | gate_skipped | `_require_gate_failure_reason` | `_skip_gate_set_fields`, `_skip_gate_post_message`, `_skip_gate_dispatch_dependents` | Skip Gate | secondary |
| 12 | stopped | cancel | cancelled | — | — | `_revert_punchlist`, `_clear_held_flag`, `_drain_queue_effect` | Cancel | danger |
| 13 | stopped | close | completed | manually_closed | `_reject_if_working`, `_reject_if_awaiting_feedback_close` | `_close_archive_and_cleanup`, `_post_close_message` | Close | secondary |
| 14 | stopped | cancel_reopen | completed | — | `_require_awaiting_feedback` | `_cancel_reopen_side_effects` | Cancel Reopen | secondary |
| 15 | completed | reopen | stopped | awaiting_feedback | — | `_reopen_side_effects` | Reopen | secondary |
| 16 | cancelled | retry | working | — | — | `_retry_launch_session` | Retry | primary |
| 17 | cancelled | resume | working | — | `_require_session_id` | `_resume_launch_session` | Resume | primary |

**Note:** `(completed, retry)` exists in the table but has no label — it's used by system code (gate retries), not shown as a dashboard button.

#### System-Initiated Actions (not shown in dashboard)

| # | From | Action | To | Reason | Side Effects |
|---|------|--------|----|--------|-------------|
| 18 | working | complete | validating | — | `_on_sdk_complete` |
| 19 | working | exhaust_turns | *dynamic* | *dynamic* | `_on_exhaust_turns` |
| 20 | working | timeout | stopped | wall_clock_timeout | `_on_timeout` |
| 21 | working | rate_limit | stopped | rate_limited | `_on_rate_limit` |
| 22 | working | error | stopped | dispatch_error | `_on_error` |
| 23 | working | signal_kill | working | — | `_on_signal_kill` |
| 24 | validating | gate_pass | completed | gate_passed | `_on_gate_pass` |
| 25 | validating | gate_fail | stopped | *dynamic* | `_on_gate_fail` |
| 26 | validating | gate_retry | working | — | *(none)* |
| 27 | validating | retry | working | — | `_retry_launch_session` |
| 28 | validating | resume | working | — | `_resume_launch_session` |

**Dynamic transitions:**
- `exhaust_turns`: goes to `validating` if gates are configured (project has `test_command`), else `stopped/turns_exhausted`
- `gate_fail` reason: comes from context — `max_test_retries`, `max_review_retries`, or `review_stalled`

#### Recovery Actions (system-initiated, not shown in dashboard)

| # | From | Action | To | Reason | Side Effects |
|---|------|--------|----|--------|-------------|
| 29 | working | recover_park | stopped | recovery_pending | `_drain_queue_effect` |
| 30 | stopped | recover_park | stopped | recovery_pending | *(none)* |
| 31 | stopped | recover_queue | ready | — | `_recover_queue_side_effects` |
| 32 | stopped | recover_fail | stopped | recovery_failed | `_recover_fail_post_message` |
| 33 | working | recover_fail | stopped | recovery_failed | `_recover_fail_post_message`, `_drain_queue_effect` |
| 34 | working | recover_cancel | cancelled | — | `_revert_punchlist` |
| 35 | stopped | recover_cancel | cancelled | — | `_revert_punchlist` |
| 36 | working | cancel | cancelled | — | (see #4 above) |

### 1.4 State Label Mapping

Every `(state, reason)` pair maps to a user-facing label, color, and pulse indicator.

| State | Reason | Label | Color | Pulse |
|-------|--------|-------|-------|-------|
| ready | *(none)* | Ready | #6b7280 (gray) | no |
| ready | held | Held | #f59e0b (amber) | no |
| ready | queued | Queued | #6b7280 (gray) | no |
| ready | blocked | Blocked | #f59e0b (amber) | no |
| working | *(none)* | Working | #3b82f6 (blue) | **yes** |
| validating | testing | Testing | #8b5cf6 (purple) | **yes** |
| validating | reviewing | Reviewing | #8b5cf6 (purple) | **yes** |
| validating | pushing | Pushing | #8b5cf6 (purple) | **yes** |
| validating | *(none)* | Validating | #8b5cf6 (purple) | **yes** |
| stopped | paused_by_user | Paused | #f59e0b (amber) | no |
| stopped | turns_exhausted | Turns Exhausted | #f59e0b (amber) | no |
| stopped | wall_clock_timeout | Timed Out | #f59e0b (amber) | no |
| stopped | rate_limited | Rate Limited | #f59e0b (amber) | no |
| stopped | max_test_retries | Tests Failed | #ef4444 (red) | no |
| stopped | max_review_retries | Review Failed | #ef4444 (red) | no |
| stopped | review_stalled | Review Stalled | #ef4444 (red) | no |
| stopped | dispatch_error | Error | #ef4444 (red) | no |
| stopped | worktree_missing | Worktree Missing | #ef4444 (red) | no |
| stopped | push_failed | Push Failed | #ef4444 (red) | no |
| stopped | awaiting_feedback | Awaiting Feedback | #f59e0b (amber) | no |
| stopped | recovery_pending | Recovering | #f59e0b (amber) | **yes** |
| stopped | recovery_failed | Recovery Failed | #ef4444 (red) | no |
| stopped | recovery_limit | Recovery Failed | #ef4444 (red) | no |
| stopped | *(none)* | Stopped | #f59e0b (amber) | no |
| completed | gate_passed | Completed | #10b981 (green) | no |
| completed | gate_skipped | Completed (Skipped) | #10b981 (green) | no |
| completed | manually_closed | Closed | #10b981 (green) | no |
| completed | *(none)* | Completed | #10b981 (green) | no |
| cancelled | *(none)* | Cancelled | #6b7280 (gray) | no |

**Ready sub-state derivation:** Ready-state reason is not stored in DB but derived at runtime by `_effective_ready_reason()`:
- `task.status == "blocked"` → reason `"blocked"`
- `task.held == True` → reason `"held"`
- `task.queued_at is not None` → reason `"queued"`
- Otherwise → reason `None`

### 1.5 Action Filtering Logic (`get_available_actions`)

The method (`lifecycle.py:1355-1406`) determines which buttons the dashboard shows:

1. Read task from DB
2. Map to effective state via `_effective_state()`
3. **Special handling for ready state:**
   - If `held`: returns `[Approve, Cancel]`
   - If `queued_at` or `blocked`: returns `[Cancel]` only
   - Otherwise: falls through to normal logic (returns `[Dispatch, Cancel]`)
4. **Normal logic:** iterate all transitions for the current effective state:
   - Skip if no `label` (hidden action)
   - Skip if `user_action=False` (system action)
   - Evaluate preconditions — skip if any precondition raises `ValueError`
   - Collect passing actions as `{name, label, style, confirm}`

**Precondition-based filtering examples:**
- `stopped/resume`: hidden if no `session_id`, no gate-resumable state, and no worktree
- `stopped/start`: hidden unless `reason == "awaiting_feedback"`
- `stopped/skip_gate`: hidden unless reason is a gate failure (`max_test_retries`, `max_review_retries`, `review_stalled`)
- `stopped/close`: hidden if `reason == "awaiting_feedback"` (use Cancel Reopen instead)
- `stopped/cancel_reopen`: hidden unless `reason == "awaiting_feedback"`

### 1.6 Dashboard Actions API Endpoint

**Endpoint:** `GET /dashboard/api/tasks/{id}/actions` (`dashboard/api.py:945-977`)

**Response format:**
```json
{
  "task_id": "project/task",
  "state": {
    "status": "stopped",
    "reason": "paused_by_user",
    "label": "Paused",
    "color": "#f59e0b",
    "pulse": false
  },
  "actions": [
    {"name": "resume", "label": "Resume", "style": "primary", "confirm": false},
    {"name": "retry", "label": "Retry", "style": "primary", "confirm": false},
    {"name": "cancel", "label": "Cancel", "style": "danger", "confirm": true}
  ]
}
```

**Implementation:** Calls `lifecycle.get_available_actions()` and `lifecycle.get_state_label()`, converts underscore action names to hyphenated format for frontend compatibility (e.g., `skip_gate` → `skip-gate`).

### 1.7 Recovery Integration

Recovery (`recovery.py`) uses 7 lifecycle transitions added specifically for it:

| Transition | Used By | Purpose |
|-----------|---------|---------|
| `(working, recover_park)` | `recover_orphaned_tasks` | Park orphans to free concurrency slots |
| `(stopped, recover_park)` | `_recover_with_resume` fallback, `_recover_single_task` | Re-park before retry |
| `(stopped, recover_queue)` | `recover_orphaned_tasks` | Queue when concurrency full |
| `(working, recover_fail)` | `recover_orphaned_tasks` exception handler | Fail on dispatch error |
| `(stopped, recover_fail)` | Flap detection, `_recover_gate_subtask`, `_recover_with_retry`, `_recover_single_task` | Mark for manual review |
| `(working, recover_cancel)` | `_recover_gate_subtask` | Cancel orphaned subtask |
| `(stopped, recover_cancel)` | `_recover_gate_subtask` | Cancel orphaned subtask (from stopped) |

**Recovery flow:**
1. `recover_orphaned_tasks()` runs at startup
2. Parks all working orphans via `recover_park` (frees concurrency)
3. Failed tasks with signal kills or no worker output are also parked
4. Each orphan is classified (gate_subtask > chain_parent > resume > retry)
5. Flap detection checks `recovery_count >= MAX_RECOVERY_ATTEMPTS` → `recover_fail`
6. Concurrency check → `recover_queue` if full
7. Dispatch via `resume_task()` or `retry_task()` (which use `lifecycle.execute()`)

**Non-status recovery DB writes (intentionally kept direct):**
- `db.update_task(task_id, recovery_priority=True)` — marks for recovery priority
- `db.update_task(task_id, recovery_count=..., last_recovery_at=...)` — tracking fields

---

## Part 2: Goal Compliance

### 2.1 Goal: 6 states with a reason field

**Status: ACHIEVED.**

The 6-state model (`ready`, `working`, `validating`, `stopped`, `completed`, `cancelled`) is fully implemented. The `reason` column exists on the `tasks` table. The `_effective_state()` method maps 12 legacy DB statuses to the 6-state model.

### 2.2 Goal: "No code outside lifecycle.py calls db.update_task(status=...)"

**Status: NOT FULLY ACHIEVED. 6 remaining direct status calls in production code.**

#### Remaining direct `db.update_task(status=...)` calls in production code

| # | File | Line | Status Set | Context |
|---|------|------|-----------|---------|
| 1 | `switchboard/server/handlers/tasks.py` | 637 | `needs-review` | `_handle_escalate` — worker escalation tool |
| 2 | `switchboard/git/operations.py` | 446 | `needs-review` | `_perform_auto_merge` — checkout failure |
| 3 | `switchboard/git/operations.py` | 470 | `needs-review` | `_perform_auto_merge` — merge conflict |
| 4 | `switchboard/git/operations.py` | 495 | `needs-review` | `_perform_auto_merge` — push failure |
| 5 | `switchboard/git/operations.py` | 504 | `merged` | `_perform_auto_merge` — success |
| 6 | `switchboard/dispatch/pr_sweep.py` | 92 | `merged` | `_handle_pr_merged` — PR sweep detected merge |

#### Direct status calls inside lifecycle.py side effects (technically inside lifecycle)

| # | File | Line | Status Set | Context |
|---|------|------|-----------|---------|
| 7 | `switchboard/dispatch/lifecycle.py` | 180 | `ready` | `_dispatch_launch_session` — reverts to ready when queued |
| 8 | `switchboard/dispatch/lifecycle.py` | 355 | `needs-review` | `_retry_launch_session` — fallback on dispatch failure |

#### Analysis

- **Items 1-6** are genuine violations of the "lifecycle owns all status transitions" principle. They should be migrated to lifecycle transitions (e.g., `escalate`, `merge`, `merge_fail`).
- **Items 7-8** are side effects within lifecycle.py itself. They bypass `execute()` but are architecturally inside the lifecycle service. Item 8 (`needs-review`) is a legacy status value that should be `stopped/dispatch_error` via a proper transition.
- **Test code** has ~120+ direct `db.update_task(status=...)` calls — this is expected and correct (test setup).

### 2.3 Goal: Single execute() method as entry point

**Status: MOSTLY ACHIEVED.**

All engine.py public operations route through `lifecycle.execute()`:
- `dispatch_task()` → `lifecycle.execute("dispatch")` or `lifecycle.execute("resume")`
- `resume_task()` → `lifecycle.execute("resume")`
- `retry_task()` → `lifecycle.execute("retry")`
- `reopen_task()` → `lifecycle.execute("reopen")`
- `cancel_reopen()` → `lifecycle.execute("cancel_reopen")`
- `start_reopened_task()` → `lifecycle.execute("start")`
- `stop_task()` → `lifecycle.execute("stop")`
- `cancel_task()` → `lifecycle.execute("cancel")`
- `skip_gate()` → `lifecycle.execute("skip_gate")`
- `advance_chain()` → `lifecycle.execute("dispatch")`
- `cancel_chain()` → `lifecycle.execute("cancel")`

**Exceptions:** The 6 direct calls listed in 2.2 above. Also, `approve_task()` does not use lifecycle for status change (it only clears the `held` flag and may call `dispatch_task()`).

### 2.4 Goal: Declarative transition table with preconditions and side effects

**Status: ACHIEVED.**

The `TRANSITIONS` dict at `lifecycle.py:915-1156` is fully declarative. Each entry is a `TransitionDef` with:
- `to_state` (static or dynamic callable)
- `reason` (static or dynamic callable)
- `preconditions` (list of async callables that raise on failure)
- `side_effects` (list of async callables)
- `label`, `style`, `confirm` (dashboard rendering hints)
- `user_action` (whether to show as a dashboard button)

### 2.5 Goal: Dashboard renders buttons from API, not hardcoded logic

**Status: ACHIEVED.**

The dashboard fetches `GET /dashboard/api/tasks/{id}/actions` which calls `lifecycle.get_available_actions()`. The response includes action names, labels, styles, and confirm flags. The frontend renders buttons directly from this response — no hardcoded button logic in the dashboard JS.

---

## Part 3: What Changed From the Design

### 3.1 Changes to the State Model

| Aspect | Original Design | As-Built | Reason |
|--------|----------------|----------|--------|
| Gate sub-machine location | Inside `validating` state | Partially — gate_status still managed by `gates.py` directly via `db.update_task(gate_status=...)` | Gate sub-machine has ~15 internal states; embedding all in lifecycle would bloat the transition table |
| `merged` status | Folded into `completed` | `merged` still exists as a DB status, mapped to `completed` by `_effective_state()` | `operations.py` and `pr_sweep.py` set it directly; not yet migrated |
| `needs-review` status | Folded into `stopped` | Still written by 3 production code paths; mapped to `stopped` by `_effective_state()` | Escalation, auto-merge failures, and retry-dispatch failures not yet migrated |
| `blocked` status | Folded into `ready` | Still exists as DB status, mapped to `ready/blocked` | Dependency tracking predates lifecycle; no migration needed since `_effective_state()` handles it |

### 3.2 Changes to Recovery Design

| Aspect | Original Design | As-Built | Reason |
|--------|----------------|----------|--------|
| Recovery action | Single `lifecycle.execute("recover")` | 7 granular actions: `recover_park`, `recover_queue`, `recover_fail`, `recover_cancel` (each from working and/or stopped) | The two-phase pattern (park all → process one-by-one) is load-bearing for concurrency management |
| Recovery state | `stopped/recovering` | `stopped/recovery_pending` for parking, `stopped/recovery_failed` for failures | More granular reasons for dashboard display |

### 3.3 Additions Not in Original Design

| Feature | Description | Where |
|---------|-------------|-------|
| Reason-aware action filtering | Preconditions filter buttons based on `task.reason`, not just state | `get_available_actions()` + precondition functions |
| Ready sub-state derivation | `_effective_ready_reason()` derives held/queued/blocked from task fields | `lifecycle.py:1238-1250` |
| Dynamic state resolvers | `to_state` and `reason` can be callables for conditional transitions | `TransitionDef.resolve_target()`, used by `exhaust_turns` and `gate_fail` |
| Cancel reopen flow | `cancel_reopen` restores saved gate state and decrements attempt | `lifecycle.py:457-478`, transition #14 |
| Signal kill transition | `(working, signal_kill) → working` — stays working but sets recovery priority | `lifecycle.py:1111-1116` |
| `approve` as pseudo-action | Approve appears in action list for held tasks but is not a lifecycle transition | `get_available_actions()` special case |
| Gate-interrupted shortcut in retry | If retry is called during active gate, re-enter gate pipeline instead of re-launching CC | `_retry_launch_session` lines 300-306 |
| Gate-passed shortcut in resume | If resuming a completed task with gate_passed_at, skip CC and re-run post-gate pipeline | `_resume_launch_session` lines 244-249 |

### 3.4 What Was Dropped

| Planned Feature | Status | Reason |
|----------------|--------|--------|
| DB migration to write new status values | Partially done | New transitions write 6-state values, but legacy code paths still write old values. `_effective_state()` handles the gap. |
| Full gate sub-machine in lifecycle | Not done | Gate status management (~15 states) stayed in `gates.py` as direct `db.update_task(gate_status=...)` calls. This is by design — gate_status is a field, not task status. |
| `(cancelled, resume) → working` (when session_id exists) | Implemented | Original design flagged this as a gap; it was implemented as transition #17 |

### 3.5 Phase 2 Correction

The original Phase 2 approach (migrate `dispatch_task` directly) failed because `lifecycle.execute()` sets `status="working"` before side effects fire, but the dispatch side effect needs to check concurrency and potentially queue instead.

**Solution implemented:** Extract 7 status-agnostic functions into `switchboard/dispatch/internals.py`:
- `check_and_queue_if_full()`
- `setup_task_worktree()`
- `resolve_session_config()`
- `build_dispatch_prompt()`
- `launch_sdk_session()`
- `collect_review_feedback()`
- `collect_reopen_feedback()`

Side effects compose these functions. The queuing edge case is handled by `_dispatch_launch_session` reverting status to `ready` inside the side effect (lifecycle.py:180) — a pragmatic workaround for the status-before-side-effect ordering.

---

## Part 4: Gaps and Issues

### 4.1 Remaining Direct Status Calls (Priority: Medium)

Six production code paths still bypass `lifecycle.execute()` for status transitions:

1. **`handlers/tasks.py:637` — `_handle_escalate`**: Sets `status="needs-review"`. Should be a `(working, escalate) → stopped/escalated` transition.

2. **`operations.py:446,470,495` — `_perform_auto_merge` failures**: Sets `status="needs-review"`. Should be `(completed, merge_fail) → stopped/merge_conflict` or similar.

3. **`operations.py:504` — `_perform_auto_merge` success**: Sets `status="merged"`. Should be `(completed, merge) → completed/merged` transition.

4. **`pr_sweep.py:92` — `_handle_pr_merged`**: Sets `status="merged"`. Same as above — should use lifecycle.

5. **`lifecycle.py:180` — `_dispatch_launch_session`**: Reverts `status="ready"` when queued. This is inside a side effect — the lifecycle set `working` first, then the side effect discovered concurrency was full and reverted. Architecturally awkward but functional.

6. **`lifecycle.py:355` — `_retry_launch_session`**: Sets `status="needs-review"` on dispatch failure. Should use `lifecycle.execute("error")` or similar instead of writing a legacy status value.

### 4.2 Dead Code / Unreachable Paths

1. **`(stopped, recovery_limit)` state label** (`lifecycle.py:1213`): The label exists for reason `"recovery_limit"` but no transition ever sets this reason. Recovery flap detection uses `recover_fail` which sets reason `"recovery_failed"`, not `"recovery_limit"`. This label is unreachable.

2. **`(stopped, worktree_missing)` state label** (`lifecycle.py:1208`): No transition sets reason `"worktree_missing"`. The worktree-missing case in `_resume_launch_session` falls back to recreating the worktree rather than transitioning to this state.

3. **`(stopped, push_failed)` state label** (`lifecycle.py:1209`): No lifecycle transition sets this reason. Push failures set `gate_status="push-failed"` via direct `db.update_task()`, not task status/reason.

4. **`(validating, pushing)` state label** (`lifecycle.py:1198`): No code sets gate_status to `"pushing"`. The push operation runs synchronously within `_on_sdk_complete` and `_on_exhaust_turns` — there's no intermediate "pushing" gate state.

### 4.3 Thin Wrappers in engine.py

These functions in `engine.py` are now thin wrappers around `lifecycle.execute()` and could potentially be inlined:

| Function | Lines | What it does beyond lifecycle.execute() |
|----------|-------|----------------------------------------|
| `resume_task()` | 679-696 | Passes `reset_recovery_count` kwarg |
| `retry_task()` | 699-714 | Passes `clean` kwarg (unused by lifecycle) |
| `reopen_task()` | 717-729 | Pure wrapper |
| `cancel_reopen()` | 732-744 | Pure wrapper |
| `start_reopened_task()` | 747-764 | Accepts `auto_test`/`auto_review` kwargs (unused by lifecycle) |
| `stop_task()` | 767-775 | Pure wrapper, returns simplified dict |
| `cancel_task()` | 778-786 | Pure wrapper, returns simplified dict |
| `skip_gate()` | 789-801 | Pure wrapper, returns simplified dict |

**Recommendation:** Keep these wrappers for now. They provide a clean import boundary — MCP handlers and other modules import from `engine`, not directly from `lifecycle`. Removing them would spread `lifecycle` imports across the codebase. However, `retry_task`'s `clean` parameter and `start_reopened_task`'s `auto_test`/`auto_review` parameters are dead — they're accepted but never forwarded.

### 4.4 Inconsistencies

1. **Legacy status values still written:** `needs-review` is written by 4 code paths (escalate, 2 auto-merge failures, retry dispatch failure). `merged` is written by 2 code paths (auto-merge success, PR sweep). These bypass lifecycle and write values that `_effective_state()` must then map.

2. **`_retry_launch_session` fallback writes legacy status:** On line 355, when dispatch fails during retry, the side effect writes `status="needs-review"` directly. This should use `lifecycle.execute("error")` to set `stopped/dispatch_error` instead.

3. **`_dispatch_launch_session` status revert:** On line 180, the side effect sets `status="ready"` directly after lifecycle already set `status="working"`. This creates a brief window where the task is "working" but then reverts to "ready". The audit log records the `working` transition but not the revert.

4. **`approve_task` is not a lifecycle transition:** It clears the `held` flag and dispatches, but the approval itself has no lifecycle transition. The audit log records "approved" manually rather than through lifecycle's audit mechanism.

### 4.5 Status Values in DB Not Handled by `_effective_state()`

The `_STATUS_MAP` handles: `pending-validation`, `needs-review`, `turns-exhausted`, `rate-limited`, `failed`, `reopened`, `merged`, `blocked`, `ready`, `working`, `validating`, `stopped`, `completed`, `cancelled`.

**Not in the map (would hit the fallback warning):**
- Any typos or corrupted values (defensive — unlikely)
- No known legitimate status values are missing from the map

### 4.6 Test Coverage

All 36 transitions have test coverage:
- `tests/test_lifecycle.py` — 29 direct transition tests
- `tests/test_lifecycle_actions.py` — dispatch/resume/retry/start/reopen action tests
- `tests/test_lifecycle_system_events.py` — system event tests (complete, exhaust_turns, timeout, rate_limit, error, signal_kill, gate_pass, gate_fail, gate_retry)
- `tests/test_crash_recovery.py` — recovery action tests (recover_park, recover_queue, recover_fail, recover_cancel) + static source check verifying recovery.py has no direct status calls
- `tests/test_reopen.py` — reopen workflow tests

**Static source checks in tests:**
- `test_lifecycle_system_events.py:630-660` — verifies migrated modules don't call `db.update_task(status=...)` directly
- `test_crash_recovery.py:774-801` — verifies `recovery.py` has zero direct status calls

---

## Summary Scorecard

| Goal | Status | Notes |
|------|--------|-------|
| 6-state model with reason field | **ACHIEVED** | All 6 states active, reason field in use |
| No status calls outside lifecycle | **PARTIAL** | 6 remaining in production code (escalate, auto-merge, PR sweep, 2 side-effect fallbacks) |
| Single execute() entry point | **MOSTLY ACHIEVED** | All engine.py operations routed through lifecycle; 6 exceptions noted above |
| Declarative transition table | **ACHIEVED** | 36 transitions, all declarative with preconditions and side effects |
| Dashboard renders from API | **ACHIEVED** | `/dashboard/api/tasks/{id}/actions` endpoint, no hardcoded buttons |
| Recovery through lifecycle | **ACHIEVED** | All 13 recovery status calls replaced with 7 lifecycle transitions |
| Full test coverage | **ACHIEVED** | All 36 transitions tested, static source checks in place |

### Recommended Next Steps

1. **Migrate escalate, auto-merge, and PR sweep** to lifecycle transitions (adds ~5 transitions)
2. **Fix `_retry_launch_session` fallback** to use lifecycle instead of writing `needs-review`
3. **Remove dead state labels** (`recovery_limit`, `worktree_missing`, `push_failed`, `pushing`)
4. **Remove dead parameters** on `retry_task(clean=)` and `start_reopened_task(auto_test=, auto_review=)`
5. **Add `(working, escalate) → stopped/escalated` transition** to lifecycle table
