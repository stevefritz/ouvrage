# Gate Recovery After SIGKILL / Restart — Analysis & Implementation Plan

## Root Cause

When a task completes its CC session, its status transitions from `working` → `validating` via the `complete` lifecycle action (lifecycle.py:1149-1153). The gate pipeline (test gate, review gate) then runs as async coroutines tracked in the in-memory `_running_gates` set.

**On SIGKILL/restart**, both the recovery sweep (`recover_orphaned_tasks()`) and the background health check (`check_stalled_tasks()`) look for stuck gates in these statuses:

```python
for status in ("completed", "pending-validation", "turns-exhausted"):  # recovery.py:179, recovery.py:574
```

**But they never check `validating` status.** Tasks that were mid-gate when killed are left in `status=validating` with `gate_status=testing` or `gate_status=reviewing`, invisible to all recovery paths.

## Detailed Trace

### Scenario 1: Task in gate_status='testing' when SIGKILL hits

**Code path:**
1. CC session completes → `lifecycle.execute(task_id, "complete")` → `status=validating` (lifecycle.py:1149)
2. `_on_sdk_complete` side effect calls `_run_test_gate(task_id, project, task)` (lifecycle.py:624)
3. `_run_test_gate` adds to `_running_gates`, stores in `_gate_tasks`, sets `gate_status="testing"` (gates.py:358-364, 382)
4. `_run_test_streaming` spawns subprocess (gates.py:112-137)
5. **SIGKILL**: Process dies. Subprocess killed. `_running_gates` lost (in-memory only).
6. **DB state**: `status=validating`, `gate_status=testing`

**Why not recovered:**
- `recover_orphaned_tasks()` line 179: iterates `("completed", "pending-validation", "turns-exhausted")` — does NOT include `"validating"`
- `check_stalled_tasks()` line 574: same three statuses — does NOT include `"validating"`
- `mark_working_for_recovery()` line 60: only marks `status=working` tasks; lines 66-71 only LOG validating tasks with active gates but don't mark them

### Scenario 2: Task in gate_status='reviewing' when SIGKILL hits

**Code path (inline subtask — current approach):**
1. Tests pass → `gate_status="test-passed"` → `_dispatch_review()` called
2. `_dispatch_review` sets `gate_status="reviewing"` (gates.py:516), adds to `_running_gates` (gates.py:505)
3. `_run_subtask()` launches SDK session for review (gates.py:759-764)
4. **SIGKILL**: Process dies. SDK session killed. `_running_gates` lost.
5. **DB state**: `status=validating`, `gate_status=reviewing`
6. **Subtask DB record**: `status=NULL` (never completed/failed)

**Why not recovered:** Same as Scenario 1 — `validating` not in the recovery sweep's status list.

**Code path (separate task review — legacy pattern):**
1. Review dispatched as separate task with `parent_task_id` set, `status=working`
2. **SIGKILL**: Task left with `status=working`
3. **This IS recovered**: `recover_orphaned_tasks()` picks it up as an orphaned working task, `_classify_orphan` returns priority 0 / method "gate_subtask", `_recover_gate_subtask` re-triggers the parent gate.

### Scenario 3: Graceful shutdown (SIGTERM)

**Code path:** `mark_working_for_recovery()` (recovery.py:43-71)
- Lines 60-63: Marks all `status=working` tasks with `recovery_priority=True`
- Lines 66-71: For `completed`, `pending-validation`, `turns-exhausted` tasks with active gate states (`testing`, `reviewing`), only LOGS them — does not mark them
- **Does NOT check `validating` status at all**

Docker sends SIGTERM, waits (default 10s), then SIGKILL. Even if SIGTERM handler runs, it doesn't handle `validating` tasks.

## What Already Works

`_resume_gate_pipeline()` (gates.py:939-1085) is the unified recovery entry point and **already handles all gate states correctly**:
- `gate=testing` → re-runs `_run_test_gate` (line 1013-1015)
- `gate=reviewing` → resets to `test-passed`, re-dispatches review (line 1038-1045)
- `gate=test-passed` → dispatches review or sets passed (line 1028-1036)
- `gate=test-failed` / `gate=review-failed` → returns False (caller should retry CC)
- `gate=None` → enters gate pipeline from top (line 998-1010)

The logic is sound. The only problem is **nobody calls it for `validating` tasks**.

## Fix: Two Changes

### Change 1: Add `"validating"` to the startup recovery sweep

**File:** `switchboard/dispatch/recovery.py`
**Function:** `recover_orphaned_tasks()` (line 179)
**Change:** Add `"validating"` to the status tuple:

```python
# Before (line 179):
for status in ("completed", "pending-validation", "turns-exhausted"):

# After:
for status in ("completed", "pending-validation", "turns-exhausted", "validating"):
```

This makes the existing gate recovery logic in lines 180-202 also sweep `validating` tasks. The inner logic already handles all gate states correctly via `_resume_gate_pipeline`.

### Change 2: Add `"validating"` to the background health check

**File:** `switchboard/dispatch/recovery.py`
**Function:** `check_stalled_tasks()` (line 574)
**Change:** Add `"validating"` to the status tuple:

```python
# Before (line 574):
for gate_status in ("completed", "pending-validation", "turns-exhausted"):

# After:
for gate_status in ("completed", "pending-validation", "turns-exhausted", "validating"):
```

This ensures that even if the startup recovery misses a task (e.g., gate becomes orphaned at runtime without a full restart), the background loop catches it.

### Change 3 (optional): Improve graceful shutdown logging

**File:** `switchboard/dispatch/recovery.py`
**Function:** `mark_working_for_recovery()` (line 66)
**Change:** Add `"validating"` to the logging sweep:

```python
# Before (line 66):
for gate_status in ("completed", "pending-validation", "turns-exhausted"):

# After:
for gate_status in ("completed", "pending-validation", "turns-exhausted", "validating"):
```

This is purely for observability — the actual recovery happens on restart via Changes 1 and 2. But it ensures SIGTERM shutdown logs include validating tasks.

## Why This Is Sufficient

1. **No new recovery logic needed.** `_resume_gate_pipeline` already handles every gate state. We just need to call it for `validating` tasks.

2. **No lifecycle changes needed.** The `validating` status and its transitions are correct. The only issue is the recovery sweep's status filter.

3. **No graceful shutdown changes needed for correctness.** The startup recovery sweep handles the actual recovery. The `_running_gates` set is empty on startup by design (all gates are orphaned after restart), so the idempotency guard in `_resume_gate_pipeline` (line 967) won't block recovery.

4. **Inline review subtasks don't need separate recovery.** They're coroutines in the parent's gate pipeline, not separate tasks. Recovering the parent via `_resume_gate_pipeline` with `gate=reviewing` re-dispatches the review (line 1038-1045).

5. **Separate-task reviews (legacy) are already recovered** via the orphaned `working` task sweep + `_recover_gate_subtask`.

## Testing Considerations

Add tests that verify:
1. A task with `status=validating, gate_status=testing` is recovered by `recover_orphaned_tasks()` (calls `_resume_gate_pipeline` which re-runs test gate)
2. A task with `status=validating, gate_status=reviewing` is recovered (calls `_resume_gate_pipeline` which resets to test-passed and re-dispatches review)
3. The `check_stalled_tasks()` background loop detects `validating` tasks with orphaned gates
4. No double-recovery: if gate is already in `_running_gates`, recovery skips it

## Summary

| What | Status | Fix |
|------|--------|-----|
| Testing gate (validating + testing) | NOT RECOVERED | Add "validating" to recovery sweep status list |
| Review gate (validating + reviewing) | NOT RECOVERED | Same fix — "validating" in status list |
| Separate-task review (working + parent_task_id) | ALREADY RECOVERED | No change needed |
| Background stall detection | MISSES VALIDATING | Add "validating" to health check status list |
| Graceful shutdown | LOGS BUT DOESN'T ACT | Optional: add "validating" to logging |

**Total changes: 2 lines of production code** (both in `recovery.py`), plus 1 optional logging line.
