# Attempt Outcome Coverage Audit Report

**Date:** 2026-04-07  
**Branch:** `audit-attempt-outcome-coverage`  
**Scope:** Map every lifecycle path that creates, finalizes, or reopens an attempt and verify correct outcome storage + human-readable labeling.

---

## 1. Coverage Matrix — Every Transition's Attempt Impact

### How `_finalize_attempt` works (lifecycle.py:1085–1091)

```python
outcome = task.get("reason") or ctx.get("_previous_status", "unknown")
await db.update_attempt(task_id, attempt, finished_at=now_iso(), outcome=outcome)
```

**Key insight**: `task.get("reason")` reads the reason that was ALREADY written to the DB by `execute()` (step 6, line 1536–1540) BEFORE side effects run. So if the TransitionDef has `reason="paused_by_user"`, then `task.reason` = `"paused_by_user"` when `_finalize_attempt` runs.

### How `_reopen_attempt` works (lifecycle.py:1077–1082)

```python
await db.update_attempt(task_id, attempt, finished_at=None, outcome=None)
```

Clears `finished_at` and `outcome` on the CURRENT attempt (before any attempt counter increment).

### Complete Transition Table

| # | From State | Action | To State | Has reason? | Creates attempt? | Finalizes attempt? | Reopens attempt? | Stored outcome | OUTCOME_DEF entry | Label | Color |
|---|-----------|--------|----------|-------------|------------------|--------------------|------------------|---------------|-------------------|-------|-------|
| 1 | ready | dispatch | working | — | ✅ (attempt 1) | — | — | — (new, open) | — | — | — |
| 2 | ready | approve | ready | — | — | — | — | — | — | — | — |
| 3 | ready | cancel | cancelled | — (cleared) | — | — | — | — | — | — | — |
| 4 | **working** | **stop** | stopped | `paused_by_user` | — | ✅ | — | `paused_by_user` | ✅ | "stopped" | #6b7280 |
| 5 | **working** | **cancel** | cancelled | `cancelled` | — | ✅ | — | `cancelled` | ✅ | "cancelled" | #6b7280 |
| 6 | **validating** | **stop** | stopped | `paused_by_user` | — | ✅ | — | `paused_by_user` | ✅ | "stopped" | #6b7280 |
| 7 | **validating** | **skip_gate** | completed | `gate_skipped` | — | ✅ | — | `gate_skipped` | ✅ | "completed" | #22c55e |
| 8 | **validating** | **cancel** | cancelled | `cancelled` | — | ✅ | — | `cancelled` | ✅ | "cancelled" | #6b7280 |
| 9 | stopped | resume | working | — (cleared) | — | — | ✅ | — (cleared) | — | — | — |
| 10 | stopped | retry | working | — (cleared) | ✅ (new attempt) | — | — | — (new, open) | — | — | — |
| 11 | stopped | start | working | — (cleared) | — | — | — | — | — | — | — |
| 12 | **stopped** | **skip_gate** | completed | `gate_skipped` | — | **❌ MISSING** | — | ⚠️ Previous stop outcome retained | — | — | — |
| 13 | stopped | cancel | cancelled | — (cleared) | — | — | — | — | — | — | — |
| 14 | stopped | close | completed | `manually_closed` | — | **❌ MISSING** | — | ⚠️ Previous stop outcome retained | — | — | — |
| 15 | completed | retry | working | — (cleared) | ✅ (new attempt) | — | — | — (new, open) | — | — | — |
| 16 | completed | reopen | stopped | `awaiting_feedback` | ✅ (new attempt via `_reopen_side_effects`) | — | — | — (new, open) | — | — | — |
| 17 | cancelled | retry | working | — (cleared) | ✅ (new attempt) | — | — | — (new, open) | — | — | — |
| 18 | cancelled | resume | working | — (cleared) | — | — | ✅ | — (cleared) | — | — | — |
| 19 | stopped | cancel_reopen | completed | — | — | — | — | ⚠️ Special: restores previous attempt state | — | — | — |
| 20 | **working** | **complete** | validating | `completed` | — | ✅ | — | `completed` | ✅ | "Completed" | #10b981 |
| 21 | **working** | **exhaust_turns** | stopped | `turns_exhausted` | — | ✅ | — | `turns_exhausted` | ✅ | "turns-exhausted" | #eab308 |
| 22 | **working** | **timeout** | stopped | `wall_clock_timeout` | — | ✅ | — | `wall_clock_timeout` | ✅ | "timeout" | #ef4444 |
| 23 | **working** | **rate_limit** | stopped | `rate_limited` | — | ✅ | — | `rate_limited` | ✅ | "rate-limited" | #eab308 |
| 24 | **working** | **error** | stopped | `dispatch_error` | — | ✅ | — | `dispatch_error` | ✅ | "failed" | #ef4444 |
| 25 | **validating** | **gate_pass** | completed | `gate_passed` | — | ✅ | — | `gate_passed` | ✅ | "completed" | #22c55e |
| 26 | **validating** | **gate_fail** | stopped | dynamic¹ | — | ✅ | — | dynamic¹ | ✅ | varies | varies |
| 27 | validating | gate_retry | working | — (cleared) | — | — | ✅ | — (cleared) | — | — | — |
| 28 | validating | retry | working | — (cleared) | ✅ (new attempt) | — | — | — (new, open) | — | — | — |
| 29 | validating | resume | working | — (cleared) | — | — | ✅ | — (cleared) | — | — | — |
| 30 | working | signal_kill | working | — | — | — | — | — | — | — | — |
| 31 | **working** | **recover_park** | stopped | `recovery_pending` | — | ✅ | — | `recovery_pending` | ✅ | "stopped" | #6b7280 |
| 32 | stopped | recover_park | stopped | `recovery_pending` | — | — | — | — | — | — | — |
| 33 | stopped | recover_queue | ready | — | — | — | — | — | — | — | — |
| 34 | stopped | recover_fail | stopped | `recovery_failed` | — | — | — | — | — | — | — |
| 35 | **working** | **recover_fail** | stopped | `recovery_failed` | — | ✅ | — | `recovery_failed` | ✅ | "failed" | #ef4444 |
| 36 | **working** | **recover_cancel** | cancelled | `cancelled` | — | ✅ | — | `cancelled` | ✅ | "cancelled" | #6b7280 |
| 37 | stopped | recover_cancel | cancelled | — (cleared) | — | — | — | — | — | — | — |

**¹ gate_fail dynamic reason:** `_gate_fail_reason` returns `ctx.get("reason", "gate_failed")`. In practice:
- Test failure after max retries: `reason="max_test_retries"` → label "Tests Failed" (#ef4444)
- Review failure after max retries: `reason="max_review_retries"` → label "Review Rejected" (#ef4444)
- Default fallback: `"gate_failed"` → label "Failed" (#ef4444)

### Notes on double finalization

The **happy path** (working → complete → validating → gate_pass) calls `_finalize_attempt` **twice**:
1. `complete`: outcome = `"completed"` (from TransitionDef reason)
2. `gate_pass`: outcome = `"gate_passed"` (overwrites)

This is benign — the second write overwrites the first with the more accurate final outcome.

---

## 2. OUTCOME_DEFINITIONS Coverage

### All possible stored outcome values vs OUTCOME_DEFINITIONS

| Outcome Value | Source | In OUTCOME_DEFINITIONS? | Label | Color |
|--------------|--------|------------------------|-------|-------|
| `gate_passed` | gate_pass transition | ✅ | "completed" | #22c55e |
| `gate_skipped` | skip_gate transition | ✅ | "completed" | #22c55e |
| `paused_by_user` | stop transitions | ✅ | "stopped" | #6b7280 |
| `dispatch_error` | error transition | ✅ | "failed" | #ef4444 |
| `wall_clock_timeout` | timeout transition | ✅ | "timeout" | #ef4444 |
| `rate_limited` | rate_limit transition | ✅ | "rate-limited" | #eab308 |
| `turns_exhausted` | exhaust_turns transition | ✅ | "turns-exhausted" | #eab308 |
| `recovery_pending` | recover_park transition | ✅ | "stopped" | #6b7280 |
| `recovery_failed` | recover_fail transition | ✅ | "failed" | #ef4444 |
| `completed` | complete transition | ✅ | "Completed" | #10b981 |
| `cancelled` | cancel transitions | ✅ | "cancelled" | #6b7280 |
| `max_test_retries` | gate_fail (tests) | ✅ | "Tests Failed" | #ef4444 |
| `max_review_retries` | gate_fail (review) | ✅ | "Review Rejected" | #ef4444 |
| `review_stalled` | gate_fail (stalled) | ✅ | "Review Stalled" | #ef4444 |
| `gate_failed` | gate_fail (default) | ✅ | "Failed" | #ef4444 |
| `awaiting_feedback` | reopen transition | ⚠️ **MISSING** | — | — |
| `manually_closed` | close transition | ⚠️ **MISSING** | — | — |

### Heuristic outcome values vs OUTCOME_DEFINITIONS

| Heuristic Value | In OUTCOME_DEFINITIONS? | Label | Color |
|----------------|------------------------|-------|-------|
| `in-progress` | ✅ | "in progress" | #eab308 |
| `retried` | ✅ | "retried" | #6b7280 |
| `success` | ✅ | "completed" | #22c55e |
| `test-failure` | ✅ | "tests failed" | #ef4444 |
| `review-rejection` | ✅ | "review rejected" | #ef4444 |
| `error` | ✅ | "failed" | #ef4444 |
| `wall-clock-timeout` | ⚠️ **MISSING** (only `wall_clock_timeout` exists) | falls through → "unknown" | #6b7280 |
| `turns-exhausted` | ⚠️ **MISSING** (only `turns_exhausted` exists) | falls through → "unknown" | #6b7280 |

---

## 3. Gaps Found

### GAP-1: Heuristic/OUTCOME_DEFINITIONS string mismatch (MEDIUM)

**File:** `switchboard/db/_helpers.py:285,287` and `switchboard/dispatch/lifecycle.py:1042`

The heuristic returns `"wall-clock-timeout"` (hyphenated) and `"turns-exhausted"` (hyphenated), but OUTCOME_DEFINITIONS only has `"wall_clock_timeout"` and `"turns_exhausted"` (underscored). When the heuristic is used as fallback, these values fall through to `_OUTCOME_FALLBACK` → "unknown" (gray).

**Impact:** Legacy attempts (before stored outcomes were wired in) that ended due to timeout or turns exhaustion show "unknown" instead of their correct labels.

**Fix:** Add hyphenated aliases to OUTCOME_DEFINITIONS:

```python
# In lifecycle.py OUTCOME_DEFINITIONS, add:
"wall-clock-timeout": {"label": "timeout", "color": "#ef4444"},
"turns-exhausted": {"label": "turns-exhausted", "color": "#eab308"},
```

### GAP-2: Heuristic "test-failure" on reopen-after-success (HIGH — reported bug)

**File:** `switchboard/db/_helpers.py:281-283`

```python
elif "PASSED" in title or "PASS" in title:
    if not is_last:
        return "test-failure"  # more attempts followed
```

When tests passed but another attempt exists (`is_last=False`), the heuristic returns `"test-failure"`. This is **semantically wrong** for the reopen-after-success scenario: tests passed, gates passed, task completed successfully, then user reopened it for revisions. The label "tests failed" is incorrect.

**Impact:** This is the exact bug reported in the task spec — `unify-end-task-dialog` attempt 1 shows "tests failed" when tests passed.

**Root cause analysis:** The heuristic assumes `not is_last` means the attempt failed and was retried. But reopen-after-success also creates subsequent attempts. The heuristic has no way to distinguish "failed and retried" from "succeeded then reopened."

**Fix:** For attempts with stored outcomes (post-PR #163), this heuristic is bypassed — the stored outcome `"gate_passed"` takes precedence. The heuristic only fires for legacy attempts without stored outcomes. The fix should:

1. **Short-term**: Check for gate_pass/completion evidence before assuming failure:
```python
elif "PASSED" in title or "PASS" in title:
    if not is_last:
        # Check if there's a subsequent "COMPLETED" status — indicates reopen, not failure
        # For now, trust the test result over the attempt ordering
        return "success"  # Tests passed — outcome is success regardless of subsequent attempts
```

2. **Long-term**: Backfill stored outcomes for legacy attempts, making the heuristic unnecessary.

### GAP-3: Missing OUTCOME_DEFINITIONS for `awaiting_feedback` (LOW)

**File:** `switchboard/dispatch/lifecycle.py:1042`

The `(completed, reopen)` transition sets `reason="awaiting_feedback"` on the task. While this reason is not stored as an attempt outcome (the old attempt was already finalized, and the new attempt is freshly created), it COULD theoretically be read if something queries `task.reason` directly.

**Note:** In practice this is not a real issue because `_reopen_side_effects` creates a new attempt (which starts open) and the old attempt was already finalized by `gate_pass`. The `awaiting_feedback` reason lives on the task, not the attempt.

**Fix (defensive):**
```python
"awaiting_feedback": {"label": "awaiting feedback", "color": "#eab308"},
```

### GAP-4: Missing OUTCOME_DEFINITIONS for `manually_closed` (LOW)

**File:** `switchboard/dispatch/lifecycle.py:1042`

The `(stopped, close)` transition sets `reason="manually_closed"` but does NOT call `_finalize_attempt`. If someone adds `_finalize_attempt` to this transition later, the outcome would fall through to "unknown".

**Fix (defensive):**
```python
"manually_closed": {"label": "closed", "color": "#6b7280"},
```

### GAP-5: `(stopped, skip_gate)` missing `_finalize_attempt` (MEDIUM)

**File:** `switchboard/dispatch/lifecycle.py:1180-1188`

```python
("stopped", "skip_gate"): TransitionDef(
    to_state="completed",
    reason="gate_skipped",
    side_effects=[_stop_gate_subprocess, _skip_gate_set_fields, _skip_gate_post_message, _skip_gate_dispatch_dependents],
    ...
),
```

This transition moves to `completed` with `reason="gate_skipped"` but does NOT call `_finalize_attempt`. The attempt record retains the outcome from when it entered `stopped` (e.g., `"paused_by_user"` or `"max_test_retries"`).

**Impact:** The attempt label shows "stopped" or "Tests Failed" instead of "completed" for skip_gate from stopped.

**Fix:** Add `_finalize_attempt` to the side_effects list:
```python
side_effects=[_stop_gate_subprocess, _skip_gate_set_fields, _skip_gate_post_message, _skip_gate_dispatch_dependents, _finalize_attempt],
```

### GAP-6: `(stopped, close)` missing `_finalize_attempt` (MEDIUM)

**File:** `switchboard/dispatch/lifecycle.py:1197-1205`

```python
("stopped", "close"): TransitionDef(
    to_state="completed",
    reason="manually_closed",
    side_effects=[_close_archive_and_cleanup, _post_close_message],
    ...
),
```

Same issue — transitions to `completed` but attempt record keeps the old stop outcome.

**Fix:** Add `_finalize_attempt` and add `"manually_closed"` to OUTCOME_DEFINITIONS:
```python
side_effects=[_close_archive_and_cleanup, _post_close_message, _finalize_attempt],
```

### GAP-7: `(stopped, cancel)` missing `_finalize_attempt` (LOW)

**File:** `switchboard/dispatch/lifecycle.py:1189-1196`

This transition does NOT finalize the attempt and does NOT set a reason. The attempt retains the outcome from the stop transition. This is arguably correct — the attempt was already finalized when it entered stopped. However, the attempt's `outcome` still reads as the stop reason (e.g., `"paused_by_user"`) rather than `"cancelled"`.

**Fix (optional):** Add `reason="cancelled"` and `_finalize_attempt`:
```python
("stopped", "cancel"): TransitionDef(
    to_state="cancelled",
    reason="cancelled",
    side_effects=[_revert_punchlist, _clear_held_flag, _drain_queue_effect, _finalize_attempt],
    ...
),
```

### GAP-8: Label inconsistency — "completed" vs "Completed" (COSMETIC)

**File:** `switchboard/dispatch/lifecycle.py:1042-1067`

- `gate_passed` → label `"completed"` (lowercase)
- `completed` → label `"Completed"` (capitalized)
- `gate_skipped` → label `"completed"` (lowercase)

Similarly:
- `max_test_retries` → label `"Tests Failed"` (capitalized)
- `test-failure` → label `"tests failed"` (lowercase)
- `gate_failed` → label `"Failed"` (capitalized)
- `failed` → label `"failed"` (lowercase)

**Fix:** Standardize to either all lowercase or all title-case. Recommend lowercase for all labels:
```python
"completed": {"label": "completed", "color": "#10b981"},
"max_test_retries": {"label": "tests failed", "color": "#ef4444"},
"max_review_retries": {"label": "review rejected", "color": "#ef4444"},
"review_stalled": {"label": "review stalled", "color": "#ef4444"},
"gate_failed": {"label": "failed", "color": "#ef4444"},
```

### GAP-9: Color inconsistency — `gate_passed` vs `completed` vs `success` (COSMETIC)

- `gate_passed` → #22c55e (green-500)
- `completed` → #10b981 (emerald-500)
- `success` → #22c55e (green-500)

All three mean "successfully completed" but use two different greens.

**Fix:** Standardize to one green. Recommend #22c55e (green-500) for all success outcomes.

---

## 4. Heuristic Audit (`_determine_attempt_outcome`)

### Code path analysis (switchboard/db/_helpers.py:267-304)

The heuristic walks messages in reverse and checks:

| Priority | Condition | Returns | In OUTCOME_DEFINITIONS? | Correct? |
|----------|-----------|---------|------------------------|----------|
| 1 | dispatcher + test-result + "FAILED"/"FAIL" in title + has_next | `"test-failure"` | ✅ | ✅ |
| 2 | dispatcher + test-result + "FAILED"/"FAIL" in title + !has_next | `"test-failure"` | ✅ | ✅ |
| 3 | dispatcher + test-result + "PASSED"/"PASS" in title + !is_last | `"test-failure"` | ✅ | **❌ WRONG** (GAP-2) |
| 4 | dispatcher + "WALL CLOCK"/"TIMEOUT" in title | `"wall-clock-timeout"` | **❌ MISSING** (GAP-1) | — |
| 5 | dispatcher + "TURNS EXHAUSTED"/"TURNS" in title | `"turns-exhausted"` | **❌ MISSING** (GAP-1) | — |
| 6 | dispatcher + status + "ERROR"/"FAILED"/"DISPATCH ERROR" in title | `"error"` | ✅ | ✅ |
| 7 | dispatcher + status + "COMPLETED" in title | `"success"` | ✅ | ✅ |
| 8 | review + "APPROVED" in title + !has_next | `"success"` | ✅ | ✅ |
| 9 | review + "CHANGES REQUESTED"/"REJECT" in title + has_next | `"review-rejection"` | ✅ | ✅ |
| 10 | review + "CHANGES REQUESTED"/"REJECT" in title + !has_next | `"review-rejection"` | ✅ | ✅ |
| 11 | fallback + has_next | `"retried"` | ✅ | ✅ |
| 12 | fallback + !has_next | `"in-progress"` | ✅ | ⚠️ see note |

**Note on path 12:** `"in-progress"` is returned when no significant terminal event is found and it's the latest attempt. The dashboard (TaskView.js:941) patches this client-side for non-latest attempts, overriding `"in progress"` to `"retried"`. This is a workaround for a heuristic limitation.

### Critical scenario: reopen-after-success (path 3)

**Scenario:** Task completes successfully (tests pass, gates pass), then user reopens it.

**What happens in the heuristic:**
1. Attempt 1 messages include a test-result with "PASSED" in the title
2. Attempt 2 exists, so `is_last=False` for attempt 1
3. Heuristic hits path 3: returns `"test-failure"` ← **WRONG**

**What SHOULD happen:** Attempt 1 should show `"gate_passed"` (from stored outcome) or `"success"` (from heuristic). Since PR #163 added stored outcomes, new attempts have `outcome="gate_passed"` stored and bypass the heuristic. But **legacy attempts created before PR #163** still fall through to the heuristic and get the wrong label.

### Scenario: tests passed + review approved + not last attempt

The heuristic checks test-result BEFORE review messages (path 3 vs path 8). So even if a review "APPROVED" message exists, the test-result "PASSED" + `!is_last` check triggers first and returns `"test-failure"`.

---

## 5. Multi-Attempt Flow Traces

### Flow A: Happy path
`dispatch → working → complete → validating → gate_pass → completed`

| Step | Transition | Attempt Effect | Outcome Written |
|------|-----------|----------------|-----------------|
| dispatch | ready→working | Create attempt 1 | — |
| complete | working→validating | Finalize attempt 1 | `"completed"` |
| gate_pass | validating→completed | Finalize attempt 1 (overwrite) | `"gate_passed"` |

**Final:** Attempt 1 outcome = `"gate_passed"` → label "completed" (green) ✅

### Flow B: Reopen after success
`...gate_pass → completed → reopen → stopped → start → working → complete → gate_pass → completed`

| Step | Transition | Attempt Effect | Outcome Written |
|------|-----------|----------------|-----------------|
| (Flow A completes) | | Attempt 1 finalized | `"gate_passed"` |
| reopen | completed→stopped | Create attempt 2 (via `_reopen_side_effects`). Attempt 1 unchanged. | Attempt 1: `"gate_passed"` (untouched) |
| start | stopped→working | No attempt effect | — |
| complete | working→validating | Finalize attempt 2 | `"completed"` |
| gate_pass | validating→completed | Finalize attempt 2 (overwrite) | `"gate_passed"` |

**Final:**
- Attempt 1: outcome = `"gate_passed"` → label "completed" (green) ✅
- Attempt 2: outcome = `"gate_passed"` → label "completed" (green) ✅

**Note:** With stored outcomes (post-PR #163), this works correctly. Legacy attempts without stored outcomes would hit the heuristic bug (GAP-2).

### Flow C: Test failure retry
`dispatch → working → complete → validating → gate_fail(max_test_retries) → stopped → retry → working → complete → gate_pass`

| Step | Transition | Attempt Effect | Outcome Written |
|------|-----------|----------------|-----------------|
| dispatch | ready→working | Create attempt 1 | — |
| complete | working→validating | Finalize attempt 1 | `"completed"` |
| gate_fail | validating→stopped | Finalize attempt 1 (overwrite) | `"max_test_retries"` |

But wait — in practice, auto-retry fires from the gate pipeline BEFORE gate_fail. The gate code calls `lifecycle.execute(task_id, "retry")` when retries remain, and `lifecycle.execute(task_id, "gate_fail")` when exhausted. So:

**If retries remain:**
| Step | Transition | Attempt Effect | Outcome Written |
|------|-----------|----------------|-----------------|
| retry | validating→working | Create attempt 2 (via `_retry_launch_session`). Attempt 1 NOT finalized here. | — |

Wait — `(validating, retry)` has `side_effects=[_retry_launch_session]` but no `_finalize_attempt`. And `_retry_launch_session` only creates the new attempt, doesn't finalize the old one.

**⚠️ GAP-10:** The `(validating, retry)` transition does NOT call `_finalize_attempt`. The old attempt was finalized by the `complete` transition with outcome `"completed"`, but it was NOT re-finalized to reflect the test failure. The attempt still shows "Completed" even though the test failed.

**If retries exhausted:**
| Step | Transition | Attempt Effect | Outcome Written |
|------|-----------|----------------|-----------------|
| gate_fail | validating→stopped | Finalize attempt 1 (overwrite `"completed"` → `"max_test_retries"`) | `"max_test_retries"` |
| retry | stopped→working | Create attempt 2 | — |
| complete | working→validating | Finalize attempt 2 | `"completed"` |
| gate_pass | validating→completed | Finalize attempt 2 (overwrite) | `"gate_passed"` |

**Final (max retries):**
- Attempt 1: `"max_test_retries"` → label "Tests Failed" (red) ✅
- Attempt 2: `"gate_passed"` → label "completed" (green) ✅

**Final (auto-retry before max):**
- Attempt 1: `"completed"` → label "Completed" (emerald) ⚠️ Should show something like "test-failure" or "retried"
- Attempt 2: `"gate_passed"` → label "completed" (green) ✅

### Flow D: Review rejection retry

Same pattern as Flow C but with review gates. The `(validating, retry)` transition from review auto-retry also lacks `_finalize_attempt`, so the old attempt shows "Completed" instead of "review rejected".

### Flow E: Stop and resume
`working → stop → stopped → resume → working → complete → gate_pass`

| Step | Transition | Attempt Effect | Outcome Written |
|------|-----------|----------------|-----------------|
| stop | working→stopped | Finalize attempt | `"paused_by_user"` |
| resume | stopped→working | Reopen attempt | cleared (None) |
| complete | working→validating | Finalize attempt | `"completed"` |
| gate_pass | validating→completed | Finalize attempt (overwrite) | `"gate_passed"` |

**Final:** Single attempt, outcome = `"gate_passed"` → label "completed" (green) ✅

### Flow F: Cancel and retry
`working → stop → stopped → cancel → cancelled → retry → working`

| Step | Transition | Attempt Effect | Outcome Written |
|------|-----------|----------------|-----------------|
| stop | working→stopped | Finalize attempt 1 | `"paused_by_user"` |
| cancel | stopped→cancelled | No finalize (GAP-7) | `"paused_by_user"` (retained) |
| retry | cancelled→working | Create attempt 2 | — |

**Final:**
- Attempt 1: `"paused_by_user"` → label "stopped" (gray) ⚠️ Would be more accurate as "cancelled"
- Attempt 2: open, in progress

### Flow G: Gate retry
`working → complete → validating → gate_retry → working → complete → gate_pass`

| Step | Transition | Attempt Effect | Outcome Written |
|------|-----------|----------------|-----------------|
| complete | working→validating | Finalize attempt | `"completed"` |
| gate_retry | validating→working | Reopen attempt | cleared (None) |
| complete | working→validating | Finalize attempt | `"completed"` |
| gate_pass | validating→completed | Finalize attempt (overwrite) | `"gate_passed"` |

**Final:** Single attempt, outcome = `"gate_passed"` → label "completed" (green) ✅

**Note:** `gate_retry` is defined in TRANSITIONS but never called from production code. Gates use `retry` (new attempt) or `resume` instead.

### Flow H: Turns exhausted
`working → exhaust_turns → stopped`

| Step | Transition | Attempt Effect | Outcome Written |
|------|-----------|----------------|-----------------|
| exhaust_turns | working→stopped | Finalize attempt | `"turns_exhausted"` |

**Final:** Attempt outcome = `"turns_exhausted"` → label "turns-exhausted" (yellow) ✅

### Flow I: Rate limited + resume
`working → rate_limit → stopped → resume → working → complete → gate_pass`

| Step | Transition | Attempt Effect | Outcome Written |
|------|-----------|----------------|-----------------|
| rate_limit | working→stopped | Finalize attempt | `"rate_limited"` |
| resume | stopped→working | Reopen attempt | cleared (None) |
| complete | working→validating | Finalize attempt | `"completed"` |
| gate_pass | validating→completed | Finalize attempt (overwrite) | `"gate_passed"` |

**Final:** Single attempt, outcome = `"gate_passed"` → label "completed" (green) ✅

---

## 6. Dashboard Rendering (TaskView.js)

**File:** `dashboard/views/TaskView.js:932-942`

```javascript
const outcomeLabel = attempt.outcome_label || 'in progress';
const outcomeColor = attempt.outcome_color || colors.yellow;

// For non-latest attempts: if still showing "in progress" (legacy data), show "retried"
const effectiveLabel = (!isLatest && outcomeLabel === 'in progress') ? 'retried' : outcomeLabel;
const effectiveColor = (!isLatest && outcomeLabel === 'in progress') ? colors.textTertiary : outcomeColor;
```

The dashboard has a client-side override: non-latest attempts showing "in progress" are displayed as "retried". This is correct behavior for the case where legacy attempts have no stored outcome and the heuristic returns "in-progress" for a non-terminal attempt.

---

## 7. Real Data Diagnostic Queries

### Query 1: Attempts with NULL outcome (will trigger heuristic)
```sql
SELECT ta.task_id, ta.attempt_number, ta.outcome, ta.finished_at, ta.started_at
FROM task_attempts ta
WHERE ta.outcome IS NULL
ORDER BY ta.task_id, ta.attempt_number;
```

### Query 2: Attempts with outcome not in OUTCOME_DEFINITIONS (will show "unknown")
```sql
SELECT ta.task_id, ta.attempt_number, ta.outcome
FROM task_attempts ta
WHERE ta.outcome IS NOT NULL
  AND ta.outcome NOT IN (
    'gate_passed', 'gate_skipped', 'paused_by_user', 'dispatch_error',
    'wall_clock_timeout', 'rate_limited', 'turns_exhausted',
    'recovery_pending', 'recovery_failed', 'in-progress', 'retried',
    'success', 'test-failure', 'review-rejection', 'error', 'failed',
    'cancelled', 'completed', 'max_test_retries', 'max_review_retries',
    'review_stalled', 'gate_failed'
  )
ORDER BY ta.task_id;
```

### Query 3: Orphaned open attempts (finished_at IS NULL on non-current attempts)
```sql
SELECT ta.task_id, ta.attempt_number, ta.outcome, ta.finished_at,
       t.current_attempt, t.status
FROM task_attempts ta
JOIN tasks t ON t.id = ta.task_id
WHERE ta.finished_at IS NULL
  AND ta.attempt_number < t.current_attempt
ORDER BY ta.task_id, ta.attempt_number;
```

### Query 4: Current attempts still open on non-working tasks
```sql
SELECT ta.task_id, ta.attempt_number, ta.outcome, ta.finished_at,
       t.status, t.reason
FROM task_attempts ta
JOIN tasks t ON t.id = ta.task_id
WHERE ta.finished_at IS NULL
  AND ta.attempt_number = t.current_attempt
  AND t.status NOT IN ('working', 'validating', 'ready')
ORDER BY ta.task_id;
```

---

## 8. Summary of All Gaps

| ID | Severity | Description | Fix Location |
|----|----------|-------------|-------------|
| GAP-1 | MEDIUM | Heuristic returns hyphenated strings not in OUTCOME_DEFINITIONS | `lifecycle.py:OUTCOME_DEFINITIONS` — add aliases |
| GAP-2 | HIGH | Heuristic returns "test-failure" for reopen-after-success | `_helpers.py:282` — fix logic |
| GAP-3 | LOW | `awaiting_feedback` not in OUTCOME_DEFINITIONS (defensive) | `lifecycle.py:OUTCOME_DEFINITIONS` — add entry |
| GAP-4 | LOW | `manually_closed` not in OUTCOME_DEFINITIONS (defensive) | `lifecycle.py:OUTCOME_DEFINITIONS` — add entry |
| GAP-5 | MEDIUM | `(stopped, skip_gate)` missing `_finalize_attempt` | `lifecycle.py:1184` — add to side_effects |
| GAP-6 | MEDIUM | `(stopped, close)` missing `_finalize_attempt` | `lifecycle.py:1201` — add to side_effects |
| GAP-7 | LOW | `(stopped, cancel)` doesn't re-finalize with "cancelled" | `lifecycle.py:1192` — add reason + finalize |
| GAP-8 | COSMETIC | Label casing inconsistency (completed vs Completed) | `lifecycle.py:OUTCOME_DEFINITIONS` — standardize |
| GAP-9 | COSMETIC | Color inconsistency (#22c55e vs #10b981) for success | `lifecycle.py:OUTCOME_DEFINITIONS` — standardize |
| GAP-10 | MEDIUM | `(validating, retry)` missing `_finalize_attempt` — old attempt shows "Completed" after test/review failure auto-retry | `lifecycle.py:1295` — add `_finalize_attempt` before `_retry_launch_session` |

### Explanation of the reported bugs

**`unify-end-task-dialog` attempt 1 shows "tests failed":**
- This is GAP-2. The attempt was created before stored outcomes were wired in (pre-PR #163), so `outcome IS NULL` and the heuristic fires. Tests passed but a later attempt exists, so the heuristic returns `"test-failure"`.

**`unify-end-task-dialog` attempt 2 shows "unknown":**
- Either the attempt has an outcome value not in OUTCOME_DEFINITIONS, or `outcome IS NULL` and the heuristic returns something unexpected. Most likely, the attempt was finalized before OUTCOME_DEFINITIONS was complete and has a stale or missing outcome.

---

## 9. Recommended Fixes (Priority Order)

### Fix 1: Add heuristic string aliases to OUTCOME_DEFINITIONS (GAP-1)

```python
# lifecycle.py OUTCOME_DEFINITIONS — add after existing entries:
"wall-clock-timeout": {"label": "timeout", "color": "#ef4444"},
"turns-exhausted": {"label": "turns-exhausted", "color": "#eab308"},
```

### Fix 2: Fix heuristic reopen-after-success logic (GAP-2)

```python
# _helpers.py:281-283 — change:
elif "PASSED" in title or "PASS" in title:
    if not is_last:
        return "test-failure"  # more attempts followed

# To:
elif "PASSED" in title or "PASS" in title:
    # Tests passed — return success regardless of subsequent attempts.
    # A subsequent attempt might be a reopen (not a retry after failure).
    return "success"
```

### Fix 3: Add `_finalize_attempt` to `(stopped, skip_gate)` (GAP-5)

```python
# lifecycle.py line 1184:
side_effects=[_stop_gate_subprocess, _skip_gate_set_fields, _skip_gate_post_message, _skip_gate_dispatch_dependents, _finalize_attempt],
```

### Fix 4: Add `_finalize_attempt` to `(stopped, close)` + OUTCOME_DEFINITIONS entry (GAP-6 + GAP-4)

```python
# lifecycle.py line 1201:
side_effects=[_close_archive_and_cleanup, _post_close_message, _finalize_attempt],

# OUTCOME_DEFINITIONS — add:
"manually_closed": {"label": "closed", "color": "#6b7280"},
```

### Fix 5: Add `_finalize_attempt` to `(validating, retry)` (GAP-10)

This is tricky because `_retry_launch_session` increments the attempt counter and creates a new attempt. `_finalize_attempt` needs to finalize the OLD attempt. Since `_finalize_attempt` reads `task.current_attempt`, it needs to run BEFORE `_retry_launch_session` increments it.

But there's a problem: the `(validating, retry)` transition has no reason set. And `_finalize_attempt` would use `task.get("reason") or ctx.get("_previous_status", "unknown")` → `_previous_status` = `"validating"`, which is not in OUTCOME_DEFINITIONS.

**Better fix:** The gate code should pass `reason` in context when calling retry:
```python
# gates.py — when calling retry after test failure:
await lifecycle.execute(task_id, "retry", triggered_by="gate",
                        source_detail="test failure auto-retry",
                        reason="test_failure_retry")
```

And add `_finalize_attempt` before `_retry_launch_session` in the transition, plus handle the reason resolution. However, this is complex — a simpler approach is to add a dedicated `_finalize_before_retry` that reads the gate_status to determine the correct outcome.

### Fix 6: Standardize label casing (GAP-8) and colors (GAP-9)

```python
"completed": {"label": "completed", "color": "#22c55e"},  # was "Completed", #10b981
"max_test_retries": {"label": "tests failed", "color": "#ef4444"},  # was "Tests Failed"
"max_review_retries": {"label": "review rejected", "color": "#ef4444"},  # was "Review Rejected"
"review_stalled": {"label": "review stalled", "color": "#ef4444"},  # was "Review Stalled"
"gate_failed": {"label": "failed", "color": "#ef4444"},  # was "Failed"
```

### Fix 7: Defensive OUTCOME_DEFINITIONS entries (GAP-3, GAP-7)

```python
"awaiting_feedback": {"label": "awaiting feedback", "color": "#eab308"},
"validating": {"label": "validating", "color": "#3b82f6"},  # fallback for _previous_status
"unknown": {"label": "unknown", "color": "#6b7280"},  # explicit entry for the "unknown" fallback
```

---

## 10. Backfill Strategy for Legacy Data

For tasks created before stored outcomes were wired in, run a one-time backfill:

```sql
-- Backfill attempts that have no stored outcome but are clearly finalized
-- (finished_at is set but outcome is NULL)
-- These will continue using the heuristic until backfilled.

-- Step 1: Identify candidates
SELECT ta.task_id, ta.attempt_number, ta.finished_at, ta.outcome
FROM task_attempts ta
WHERE ta.outcome IS NULL AND ta.finished_at IS NOT NULL;

-- Step 2: For each, examine the task's final reason and stored gate state
-- to determine the correct outcome. This requires application logic
-- (the heuristic, but with the fixes applied).
```

A proper backfill script should iterate over these rows, load the task's messages for that attempt, and apply the corrected heuristic logic to set the stored outcome. This eliminates future dependence on the heuristic.
