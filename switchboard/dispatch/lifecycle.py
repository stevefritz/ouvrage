"""TaskLifecycle service — owns ALL task state transitions.

Single entry point for state changes. Contains the transition table,
effective state mapper, state labels, and the execute() method.

cancel, close, skip_gate, and stop are routed through this service.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable

import switchboard.db as db
from switchboard.db.audit import write_audit_log

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Side-effect functions for cancel / close / skip_gate
# ---------------------------------------------------------------------------


async def _cancel_running_process(task: dict, **ctx: Any) -> None:
    """Find and cancel the asyncio task from _running_tasks."""
    from switchboard.dispatch._state import _running_tasks

    task_id = task["id"]
    task_name = f"sdk-session-{task_id}"
    for t in list(_running_tasks):
        if t.get_name() == task_name and not t.done():
            t.cancel()
            logger.info("Cancelled asyncio task for %s", task_id)
            return
    # Only warn if the task was previously in working state
    prev = ctx.get("_previous_status")
    if prev == "working":
        logger.warning(
            "Could not find running asyncio task for %s — it may have been lost on restart",
            task_id,
        )


async def _revert_punchlist(task: dict, **ctx: Any) -> None:
    """Revert any punchlist items claimed by this task back to 'open'."""
    reverted = await db.revert_punchlist_items_for_task(task["id"])
    if reverted:
        logger.info("Task %s: reverted %d punchlist item(s) on cancel", task["id"], reverted)


async def _drain_queue_effect(task: dict, **ctx: Any) -> None:
    """A slot freed up — drain the FIFO queue."""
    from switchboard.dispatch.queue import _drain_queue
    await _drain_queue()


async def _clear_held_flag(task: dict, **ctx: Any) -> None:
    """Clear the held flag on cancel/close."""
    await db.update_task(task["id"], held=False)


async def _close_archive_and_cleanup(task: dict, **ctx: Any) -> None:
    """Archive logs + cleanup worktree + clear extra fields for close."""
    from switchboard.dispatch.engine import archive_task_logs
    from switchboard.git.worktree import cleanup_worktree

    project = await db.get_project(task["project_id"])
    if project:
        await archive_task_logs(task, project, "close")
        await cleanup_worktree(project, task, False)

    await db.update_task(task["id"], gate_passed_at=None, held=False, worktree_path=None)


async def _post_close_message(task: dict, **ctx: Any) -> None:
    """Post 'Manually closed' status message."""
    await db.post_task_message(
        task_id=task["id"], author="dispatcher", type="status",
        title="Manually closed",
        content="Task was manually closed — no gates or chain actions triggered.",
    )


async def _skip_gate_set_fields(task: dict, **ctx: Any) -> None:
    """Set gate_status=passed and gate_passed_at for skip_gate."""
    await db.update_task(task["id"], gate_status="passed", gate_passed_at=db.now_iso())


async def _skip_gate_post_message(task: dict, **ctx: Any) -> None:
    """Post 'Gate skipped' status message."""
    gate = task.get("gate_status") or "unknown"
    gate_label = gate.replace("-", " ").replace("_", " ")
    await db.post_task_message(
        task_id=task["id"], author="dispatcher", type="status",
        title="Gate skipped",
        content=f"Gate manually bypassed by user (was: {gate_label}).",
    )


async def _skip_gate_dispatch_dependents(task: dict, **ctx: Any) -> None:
    """Trigger chain advancement after gate skip."""
    from switchboard.dispatch.engine import _check_and_dispatch_dependents
    await _check_and_dispatch_dependents(task["id"])


# ---------------------------------------------------------------------------
# Side-effect functions for stop
# ---------------------------------------------------------------------------


async def _stop_cc_session(task: dict, **ctx: Any) -> None:
    """Kill the running CC process, preserve session_id."""
    from switchboard.dispatch._state import _running_tasks, _active_clients
    from switchboard.dispatch.sdk_session import _open_shared
    import json

    task_id = task["id"]
    task_name = f"sdk-session-{task_id}"
    for t in list(_running_tasks):
        if t.get_name() == task_name and not t.done():
            t.cancel()
            _running_tasks.discard(t)
            logger.info("Stop: cancelled asyncio task for %s", task_id)
            break

    # Remove from active clients
    _active_clients.pop(task_id, None)

    # Write stop marker to session log for UI continuity
    worktree = task.get("worktree_path")
    if worktree:
        log_path = Path(worktree) / ".switchboard" / "session.jsonl"
        if log_path.exists():
            triggered_by = ctx.get("triggered_by", "user")
            entry = {
                "timestamp": db.now_iso(),
                "type": "SystemMessage",
                "subtype": "stop",
                "stopped_by": triggered_by,
            }
            try:
                with _open_shared(log_path) as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception:
                pass


async def _stop_gate_subprocess(task: dict, **ctx: Any) -> None:
    """Kill the running test or review subprocess, preserve gate_status."""
    from switchboard.dispatch._state import _running_gates, _gate_tasks

    task_id = task["id"]
    _running_gates.discard(task_id)

    # Cancel the gate asyncio task if tracked
    gate_task = _gate_tasks.pop(task_id, None)
    if gate_task and not gate_task.done():
        gate_task.cancel()
        logger.info("Stop: cancelled gate asyncio task for %s", task_id)
    else:
        logger.info("Stop: no active gate task found for %s (already finished or untracked)", task_id)


async def _post_stop_message(task: dict, **ctx: Any) -> None:
    """Post 'Task stopped' status message."""
    await db.post_task_message(
        task_id=task["id"], author="dispatcher", type="status",
        title="Task stopped",
        content="Task paused by user. Session preserved — click Resume to continue, or Retry for a fresh session.",
    )


# ---------------------------------------------------------------------------
# Side-effect functions for dispatch
# ---------------------------------------------------------------------------


async def _dispatch_launch_session(task: dict, **ctx: Any) -> None:
    """Launch a CC session for a newly dispatched task."""
    import os
    from switchboard.dispatch.internals import (
        check_and_queue_if_full, setup_task_worktree,
        build_dispatch_prompt, launch_sdk_session, resolve_session_config,
        ensure_credential_helper,
    )
    from switchboard.notifications import slack as notify

    task_id = task["id"]

    # Concurrency check — may queue instead
    if await check_and_queue_if_full(task_id):
        # Task was queued — revert status back to ready
        await db.update_task(task_id, status="ready", queued_at=db.now_iso())
        return

    project = await db.get_project(task["project_id"])

    try:
        # Setup worktree (calls setup_credential_helper internally)
        worktree_path = await setup_task_worktree(project, task)
        # Ensure credential helper is current — idempotent, defensive call
        await ensure_credential_helper(worktree_path, task)
        dispatch_count = (task.get("dispatch_count") or 0) + 1
        await db.update_task(task_id,
            worktree_path=worktree_path,
            dispatch_count=dispatch_count,
            last_activity=db.now_iso(),
        )

        # Build prompt
        prompt = await build_dispatch_prompt(
            project, task,
            escalation_criteria=ctx.get("escalation_criteria"),
            review_feedback=ctx.get("review_feedback"),
        )

        # Create attempt record for attempt 1
        current_attempt = task.get("current_attempt") or 1
        await db.create_attempt(task_id, current_attempt)

        # Resolve config + launch
        config = resolve_session_config(task, project)
        await launch_sdk_session(
            task_id=task_id, prompt=prompt,
            worktree_path=worktree_path, **config,
        )
    except Exception as exc:
        logger.error("Dispatch failed for %s: %s", task_id, exc)
        await db.update_task(task_id, status="stopped", reason="dispatch_failed")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Dispatch failed",
            content=f"Failed to launch session: {exc}",
        )

    # Notify Slack
    checklist_items = await db.get_checklist(task_id)
    spec_content = None
    pinned = await db.get_task_pinned(task_id)
    if pinned:
        spec_content = pinned["content"]
    await notify.task_dispatched(
        task_id=task_id, goal=task["goal"], project_id=task["project_id"],
        checklist_total=len(checklist_items or []),
        checklist=checklist_items,
        spec=spec_content,
        resumed=False,
    )

    # Clear queued_at since we've dispatched
    if task.get("queued_at"):
        await db.update_task(task_id, queued_at=None)


# ---------------------------------------------------------------------------
# Side-effect functions for resume
# ---------------------------------------------------------------------------


async def _resume_launch_session(task: dict, **ctx: Any) -> None:
    """Resume a stopped/cancelled task's CC session."""
    import os
    from switchboard.dispatch.internals import (
        checkout_existing_worktree, launch_sdk_session, resolve_session_config,
        ensure_credential_helper,
    )
    from switchboard.dispatch.sdk_session import _build_resume_prompt

    task_id = task["id"]
    prev_status = ctx.get("_previous_status")

    # Apply per-dispatch gate overrides if provided
    auto_test = ctx.get("auto_test")
    auto_review = ctx.get("auto_review")
    if auto_test is not None or auto_review is not None:
        updates = {}
        if auto_test is not None:
            updates["auto_test"] = auto_test
        if auto_review is not None:
            updates["auto_review"] = auto_review
        await db.update_task(task_id, **updates)
        task.update(updates)

    # Gate-passed shortcut: re-trigger post-gate pipeline instead of CC launch
    if (task.get("gate_passed_at")
            and prev_status in ("completed", "merged", "pending-validation")
            and task.get("pr_status") != "conflict"):
        from switchboard.dispatch.engine import _check_and_dispatch_dependents
        await _check_and_dispatch_dependents(task_id)
        return

    # DO NOT clear gate_status or gate_retries (Bug #2 fix)
    # Only clear pr_status and optionally recovery_count
    updates = {}
    if task.get("pr_status"):
        updates["pr_status"] = None
    if ctx.get("reset_recovery_count", True) and task.get("recovery_count"):
        updates["recovery_count"] = 0
    if updates:
        await db.update_task(task_id, **updates)

    project = await db.get_project(task["project_id"])

    # Post resume message
    triggered_by = ctx.get("triggered_by", "user")
    await db.post_task_message(
        task_id=task_id, author="dispatcher", type="status",
        title="Resumed",
        content=f"Session resumed by {triggered_by}.",
    )

    # Worktree check — checkout from origin if missing
    worktree_path = task.get("worktree_path")
    if not worktree_path or not os.path.exists(worktree_path):
        worktree_path = await checkout_existing_worktree(project, task)
        await db.update_task(task_id, worktree_path=worktree_path)

    # Ensure credential helper is current — idempotent, recreates if missing or stale.
    # Critical: if the worktree already existed above, setup_credential_helper was NOT
    # called (checkout_existing_worktree was skipped), so we must call it unconditionally.
    await ensure_credential_helper(worktree_path, task)

    # Build resume prompt
    prompt = await _build_resume_prompt(task_id)

    # Launch with session_id for resume — try attempt record first, fall back to task-level
    current_attempt = task.get("current_attempt") or 1
    attempt = await db.get_attempt(task_id, current_attempt)
    session_id = (attempt.get("session_id") if attempt else None) or task.get("session_id")

    config = resolve_session_config(task, project)
    await launch_sdk_session(
        task_id=task_id, prompt=prompt, worktree_path=worktree_path,
        session_id=session_id, is_resume=True, **config,
    )


# ---------------------------------------------------------------------------
# Side-effect functions for retry
# ---------------------------------------------------------------------------


async def _retry_launch_session(task: dict, **ctx: Any) -> None:
    """Launch a CC session for a retried task, forking from the previous attempt."""
    import os
    from switchboard.dispatch.internals import (
        setup_task_worktree, build_dispatch_prompt,
        launch_sdk_session, resolve_session_config,
        collect_review_feedback, ensure_credential_helper,
    )
    from switchboard.dispatch.engine import (
        archive_task_logs, _invalidate_chain,
    )

    task_id = task["id"]

    # Gate-interrupted shortcut: re-enter gate pipeline, don't re-run CC
    INTERRUPTED = ("testing", "reviewing", "test-passed")
    if (not task.get("gate_passed_at")
            and task.get("gate_status") in INTERRUPTED):
        from switchboard.dispatch.gates import _resume_gate_pipeline
        await _resume_gate_pipeline(task_id, reason="retry")
        return

    project = await db.get_project(task["project_id"])

    # Archive logs
    if project:
        await archive_task_logs(task, project, "retry")

    # Revert punchlist
    await db.revert_punchlist_items_for_task(task_id)

    # Look up previous attempt's session_id for forking before incrementing
    current_attempt = task.get("current_attempt") or 1

    # Gate-triggered retries (test failure, review rejection) default to fresh
    # sessions — no fork. This ensures test output and review feedback in the
    # prompt are never compacted away by SDK context summarisation on long tasks.
    triggered_by = ctx.get("triggered_by", "")
    gate_triggered = triggered_by in ("gate", "review")

    if gate_triggered:
        fork_session_id = None
    else:
        fork_session_id = await db.get_previous_attempt_session_id(task_id, current_attempt + 1)
        # Fallback: if no attempt record, use task-level session_id
        if not fork_session_id:
            fork_session_id = task.get("session_id")

    # Increment attempt + clear gate state (keep session_id for reference)
    new_attempt = current_attempt + 1
    await db.update_task(task_id,
        gate_status=None, gate_passed_at=None,
        gate_retries=0, current_attempt=new_attempt, held=False,
    )

    # Create attempt record for the new attempt
    await db.create_attempt(task_id, new_attempt)

    # Post message
    fork_note = "forked from previous session" if fork_session_id else "fresh session"
    await db.post_task_message(
        task_id=task_id, author="switchboard", type="status",
        title=f"Attempt {new_attempt} starting",
        content=f"Attempt {new_attempt} starting — {fork_note}.",
    )

    # Invalidate chain
    dependents = await db.get_dependents(task_id)
    if dependents:
        await _invalidate_chain(task_id)

    # Collect feedback
    review_feedback = await collect_review_feedback(task_id)

    # Setup worktree + build prompt + launch
    # Wrap in try/except to avoid ghost "working" tasks if launch fails
    try:
        worktree_path = task.get("worktree_path")
        if not worktree_path or not os.path.exists(worktree_path):
            worktree_path = await setup_task_worktree(project, task)
            await db.update_task(task_id, worktree_path=worktree_path)

        # Ensure credential helper is current — idempotent, recreates if missing or stale.
        # Critical: if the worktree already existed above, setup_credential_helper was NOT
        # called (setup_task_worktree was skipped), so we must call it unconditionally.
        await ensure_credential_helper(worktree_path, task)

        prompt = await build_dispatch_prompt(project, task, review_feedback=review_feedback)
        config = resolve_session_config(task, project)
        await launch_sdk_session(
            task_id=task_id, prompt=prompt,
            worktree_path=worktree_path,
            fork_session_id=fork_session_id,
            **config,
        )
    except Exception as exc:
        logger.error("Auto-retry dispatch failed for %s: %s", task_id, exc)
        await db.update_task(task_id, status="needs-review")
        await db.post_task_message(
            task_id=task_id, author="switchboard", type="status",
            title="Auto-retry dispatch failed",
            content=f"Dispatch failed during retry: {exc}",
        )


# ---------------------------------------------------------------------------
# Side-effect functions for reopen
# ---------------------------------------------------------------------------


async def _reopen_side_effects(task: dict, **ctx: Any) -> None:
    """Increment attempt, clear gate state, save reopen state, post message.

    Does NOT clear session_id — the previous attempt's session_id is preserved
    on the task record so _start_launch_session can fork from it.
    """
    task_id = task["id"]
    prev_status = ctx.get("_previous_status")

    # Read pre-transition task to get gate state before lifecycle cleared reason
    # We need the original task's gate_status and gate_passed_at
    new_attempt = (task.get("current_attempt") or 1) + 1
    await db.update_task(
        task_id,
        current_attempt=new_attempt,
        gate_status=None,
        gate_passed_at=None,
        gate_retries=0,
        reopen_saved_gate_status=ctx.get("_saved_gate_status"),
        reopen_saved_gate_passed_at=ctx.get("_saved_gate_passed_at"),
    )

    # Create attempt record for the new attempt
    await db.create_attempt(task_id, new_attempt)

    await db.post_task_message(
        task_id=task_id, author="switchboard", type="status",
        title="Task reopened — awaiting feedback",
        content="Task reopened for revisions. Post feedback below, then click Start.",
    )


# ---------------------------------------------------------------------------
# Side-effect functions for start (reopened → working)
# ---------------------------------------------------------------------------


async def _start_launch_session(task: dict, **ctx: Any) -> None:
    """Collect reopen feedback, checkout existing branch, invalidate chain, launch CC (forking from previous attempt)."""
    import os
    from switchboard.dispatch.internals import (
        checkout_existing_worktree, build_dispatch_prompt,
        launch_sdk_session, resolve_session_config,
        collect_reopen_feedback, ensure_credential_helper,
    )
    from switchboard.dispatch.engine import _invalidate_chain
    from switchboard.notifications import slack as notify

    task_id = task["id"]
    current_attempt = task.get("current_attempt") or 1

    # Apply per-dispatch gate overrides (from dashboard checkboxes)
    auto_test = ctx.get("auto_test")
    auto_review = ctx.get("auto_review")
    if auto_test is not None or auto_review is not None:
        updates = {}
        if auto_test is not None:
            updates["auto_test"] = auto_test
        if auto_review is not None:
            updates["auto_review"] = auto_review
        await db.update_task(task_id, **updates)
        task.update(updates)

    # Look up previous attempt's session_id for forking
    fork_session_id = await db.get_previous_attempt_session_id(task_id, current_attempt)
    # Fallback: use task-level session_id if no attempt record
    if not fork_session_id:
        fork_session_id = task.get("session_id")

    # Collect reopen feedback
    review_feedback = await collect_reopen_feedback(task_id, current_attempt)

    # Post "Attempt N starting..."
    fork_note = "forked from previous session" if fork_session_id else "fresh session"
    await db.post_task_message(
        task_id=task_id, author="switchboard", type="status",
        title=f"Attempt {current_attempt} starting",
        content=f"Attempt {current_attempt} starting — {fork_note}.",
    )

    # Invalidate downstream chain
    dependents = await db.get_dependents(task_id)
    if dependents:
        await _invalidate_chain(task_id)

    project = await db.get_project(task["project_id"])

    # Checkout existing branch from origin (no rebase, no depends_on logic)
    worktree_path = task.get("worktree_path")
    if not worktree_path or not os.path.exists(worktree_path):
        worktree_path = await checkout_existing_worktree(project, task)
        await db.update_task(task_id, worktree_path=worktree_path)

    # Ensure credential helper is current — idempotent, recreates if missing or stale.
    # Critical: if the worktree already existed above, setup_credential_helper was NOT
    # called (checkout_existing_worktree was skipped), so we must call it unconditionally.
    await ensure_credential_helper(worktree_path, task)

    # Build prompt with feedback + launch
    prompt = await build_dispatch_prompt(project, task, review_feedback=review_feedback)
    config = resolve_session_config(task, project)
    await launch_sdk_session(
        task_id=task_id, prompt=prompt,
        worktree_path=worktree_path,
        fork_session_id=fork_session_id,
        **config,
    )

    # Notify
    await notify.task_attempt_starting(task_id, current_attempt, task["goal"])


# ---------------------------------------------------------------------------
# Side-effect functions for cancel_reopen
# ---------------------------------------------------------------------------


async def _cancel_reopen_side_effects(task: dict, **ctx: Any) -> None:
    """Restore saved gate state, decrement attempt, delete reopened messages."""
    task_id = task["id"]
    current_attempt = task.get("current_attempt") or 1
    prev_attempt = max(1, current_attempt - 1)

    # Delete messages stamped to the current (reopened) attempt
    async with db.get_db() as conn:
        await conn.execute(
            "DELETE FROM messages WHERE task_id = ? AND attempt_number = ?",
            (task_id, current_attempt),
        )
        await conn.commit()

    await db.update_task(
        task_id,
        current_attempt=prev_attempt,
        gate_status=task.get("reopen_saved_gate_status"),
        gate_passed_at=task.get("reopen_saved_gate_passed_at"),
        reopen_saved_gate_status=None,
        reopen_saved_gate_passed_at=None,
    )


# ---------------------------------------------------------------------------
# Side-effect functions for system events (SDK session outcomes)
# ---------------------------------------------------------------------------


async def _on_sdk_complete(task: dict, **ctx: Any) -> None:
    """SDK session completed normally — update usage, push, enter gate pipeline, notify."""
    from switchboard.dispatch.engine import _update_usage, _check_and_dispatch_dependents
    from switchboard.dispatch.gates import _run_test_gate, _dispatch_review
    from switchboard.git.operations import _ensure_branch_pushed
    from switchboard.notifications import slack as notify

    task_id = task["id"]
    result_msg = ctx.get("result_msg")

    # Update usage from result
    if result_msg:
        await _update_usage(task_id, result_msg)

    # Post completion message
    if result_msg:
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Task completed",
            content=f"CC session completed successfully.\n\n"
                    f"Turns: {result_msg.num_turns} | "
                    f"Duration: {result_msg.duration_ms / 1000:.0f}s | "
                    f"Cost: ${result_msg.total_cost_usd or 0:.4f}\n\n"
                    f"Result: {result_msg.result or '(no result)'}",
        )

    # Notify
    checklist = await db.get_checklist(task_id)
    done = sum(1 for c in checklist if c.get("done"))
    if result_msg:
        await notify.task_completed(
            task_id=task_id,
            turns=result_msg.num_turns,
            duration_s=(result_msg.duration_ms or 0) / 1000,
            cost_usd=result_msg.total_cost_usd or 0,
            checklist_done=done,
            checklist_total=len(checklist),
            result_preview=result_msg.result,
        )

    # Re-read task after status change
    task = await db.get_task(task_id)

    # Auto-push branch before gate pipeline
    push_ok = await _ensure_branch_pushed(task_id, task)
    if not push_ok:
        await db.update_task(task_id, gate_status="push-failed")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Gate pipeline blocked — push failed",
            content="Push failed. Gates will not run until code is pushed. "
                    "Fix the PAT or push manually, then resume.",
        )
        return

    # Check if this is a review task — process result on parent
    if task.get("parent_task_id"):
        from switchboard.dispatch.gates import _process_review_result
        await _process_review_result(task_id, task["parent_task_id"])
    elif task.get("gate_passed_at"):
        # Gate already passed previously — re-trigger post-gate pipeline
        logger.info("Task %s: gate already passed, re-triggering post-gate pipeline", task_id)
        await _check_and_dispatch_dependents(task_id)
    else:
        # First-pass completion — run the gate pipeline
        project = await db.get_project(task["project_id"])
        if task.get("auto_test") and project and project.get("test_command"):
            await _run_test_gate(task_id, project, task)
        elif task.get("auto_review"):
            await _dispatch_review(task_id, project, task)
        else:
            # No gates configured — pass straight through via lifecycle
            await lifecycle.execute(task_id, "gate_pass",
                triggered_by="gate-pipeline",
                source_detail="_on_complete_enter_gate (no gates configured)",
            )


async def _on_exhaust_turns(task: dict, **ctx: Any) -> None:
    """Turns exhausted — post message, push branch to preserve work."""
    from switchboard.dispatch.engine import _update_usage
    from switchboard.git.operations import _ensure_branch_pushed
    from switchboard.notifications import slack as notify

    task_id = task["id"]
    result_msg = ctx.get("result_msg")

    # Update usage
    if result_msg:
        await _update_usage(task_id, result_msg)

    # Post message
    if result_msg:
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Turns exhausted",
            content=f"CC session hit the turn limit.\n\n"
                    f"Turns: {result_msg.num_turns} | "
                    f"Cost: ${result_msg.total_cost_usd or 0:.4f}\n\n"
                    f"Work is preserved in the worktree. Resume to continue with the same session.",
        )

    # Re-read task after status change
    task = await db.get_task(task_id)

    # Push branch to preserve work
    await _ensure_branch_pushed(task_id, task)

    # Notify — task is stopped, needs user to resume
    await notify.task_needs_review(
        task_id=task_id,
        reason="Turns exhausted. Resume to continue.",
    )


async def _on_timeout(task: dict, **ctx: Any) -> None:
    """Wall clock timeout hit — post message, notify, drain queue."""
    from switchboard.dispatch.queue import _drain_queue
    from switchboard.notifications import slack as notify

    task_id = task["id"]
    max_wall_clock = ctx.get("max_wall_clock_minutes", "?")

    await db.post_task_message(
        task_id=task_id, author="dispatcher", type="status",
        title="Wall clock timeout",
        content=f"Task hit the {max_wall_clock} minute wall clock limit. "
                "Work is preserved in the worktree. Resume or adjust limits.",
    )
    await notify.task_needs_review(
        task_id=task_id,
        reason=f"Wall clock timeout ({max_wall_clock}m). Work preserved in worktree.",
    )
    await _drain_queue()


async def _on_rate_limit(task: dict, **ctx: Any) -> None:
    """API rate limit hit — set retry_after, post message, drain queue."""
    from switchboard.dispatch.queue import _drain_queue

    task_id = task["id"]
    retry_after_iso = ctx.get("retry_after")
    reset_info = ctx.get("reset_info", "")
    result_msg = ctx.get("result_msg")

    if retry_after_iso:
        await db.update_task(task_id, retry_after=retry_after_iso)

    num_turns = result_msg.num_turns if result_msg else "?"
    cost = f"${result_msg.total_cost_usd or 0:.4f}" if result_msg else "?"

    await db.post_task_message(
        task_id=task_id, author="dispatcher", type="status",
        title="Rate limited",
        content=f"CC hit usage limits.{reset_info}\n\n"
                f"Turns: {num_turns} | Cost: {cost}\n\n"
                f"Work is preserved.{' Auto-retry scheduled.' if retry_after_iso else ' Retry manually after limits reset.'}",
    )
    await _drain_queue()


async def _on_error(task: dict, **ctx: Any) -> None:
    """SDK error or exception — post message, notify, drain queue."""
    from switchboard.dispatch.queue import _drain_queue
    from switchboard.notifications import slack as notify

    task_id = task["id"]
    result_msg = ctx.get("result_msg")
    error_message = ctx.get("error_message")

    if result_msg:
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Task failed",
            content=f"CC session ended with error.\n\nStop reason: {result_msg.stop_reason}\n"
                    f"Turns: {result_msg.num_turns}\n\n"
                    f"Result: {result_msg.result or '(no result)'}",
        )
        await notify.task_failed(
            task_id=task_id,
            error=result_msg.result or result_msg.stop_reason or "Unknown error",
            turns=result_msg.num_turns,
            cost_usd=result_msg.total_cost_usd or 0,
        )
    elif error_message:
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title=ctx.get("error_title", "Dispatch error"),
            content=ctx.get("error_content", f"SDK session raised an exception:\n\n```\n{error_message}\n```"),
        )
        await notify.task_failed(task_id=task_id, error=error_message)
    else:
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Session ended without result",
            content="CC session ended but no ResultMessage was received. Check logs.",
        )
        await notify.task_needs_review(
            task_id=task_id, reason="Session ended without a ResultMessage. Check logs.",
        )

    # Handle review task crash — still try to process review
    task = await db.get_task(task_id)
    if task and task.get("parent_task_id"):
        try:
            from switchboard.dispatch.gates import _process_review_result
            await _process_review_result(task_id, task["parent_task_id"])
        except Exception:
            logger.exception("Failed to process review result for crashed review task %s", task_id)

    await _drain_queue()


async def _recover_queue_side_effects(task: dict, **ctx: Any) -> None:
    """Queue a recovering task when concurrency is full."""
    await db.update_task(task["id"], queued_at=db.now_iso(), recovery_priority=True)


async def _recover_fail_post_message(task: dict, **ctx: Any) -> None:
    """Post a message explaining why recovery failed."""
    message = ctx.get("fail_message")
    title = ctx.get("fail_title", "Recovery failed")
    if message:
        await db.post_task_message(
            task_id=task["id"], author="dispatcher", type="status",
            title=title, content=message,
        )


async def _on_signal_kill(task: dict, **ctx: Any) -> None:
    """SIGTERM/SIGKILL recovery — set recovery_priority, post message."""
    task_id = task["id"]
    error_str = ctx.get("error_message", "Unknown signal")

    await db.update_task(task_id, recovery_priority=True)
    await db.post_task_message(
        task_id=task_id, author="dispatcher", type="status",
        title="Session killed by signal",
        content=f"CC process was killed externally (likely service restart).\n"
                f"Task will auto-resume on next startup.\n\n```\n{error_str}\n```",
    )


# ---------------------------------------------------------------------------
# Side-effect functions for gate outcomes
# ---------------------------------------------------------------------------


async def _on_gate_pass(task: dict, **ctx: Any) -> None:
    """All gates passed — set gate_passed_at, resolve punchlist, chain advance, drain queue."""
    from switchboard.dispatch.engine import _check_and_dispatch_dependents
    from switchboard.dispatch.queue import _drain_queue

    task_id = task["id"]

    await db.update_task(task_id, gate_status="passed", gate_passed_at=db.now_iso())

    # Resolve punchlist items claimed by this task
    resolved = await db.resolve_punchlist_items_for_task(task_id)
    if resolved:
        logger.info("Task %s: resolved %d punchlist item(s) on gate pass", task_id, resolved)

    # Chain advancement
    await _check_and_dispatch_dependents(task_id)

    # Drain queue (slot freed)
    await _drain_queue()


async def _on_gate_fail(task: dict, **ctx: Any) -> None:
    """Gate failed after max retries — notify for manual review."""
    from switchboard.notifications import slack as notify

    task_id = task["id"]
    reason = ctx.get("reason", "gate_failed")

    await notify.task_needs_review(task_id=task_id, reason=reason)


# ---------------------------------------------------------------------------
# Precondition functions
# ---------------------------------------------------------------------------


async def _reject_if_working(task: dict, **ctx: Any) -> None:
    """Precondition: reject close if task is still working."""
    if task["status"] == "working":
        raise ValueError(
            f"Task '{task['id']}' is still running. Cancel it first, then close."
        )


async def _require_session_or_gate_resumable(task: dict, **ctx: Any) -> None:
    """Resume from stopped requires session_id, gate-resumable state, or worktree."""
    if task.get("session_id"):
        return
    GATE_RESUMABLE = {"testing", "reviewing", "test-passed", "test-failed", "review-failed"}
    if task.get("gate_status") in GATE_RESUMABLE:
        return
    if task.get("worktree_path"):
        return  # Can use continue_conversation=True as fallback
    raise ValueError(f"Task '{task['id']}' has no session to resume. Use retry.")


async def _require_session_id(task: dict, **ctx: Any) -> None:
    """Cancelled tasks can only resume if session_id exists."""
    if not task.get("session_id"):
        raise ValueError(f"Task '{task['id']}' has no session_id. Use retry for a fresh session.")


async def _reject_awaiting_feedback(task: dict, **ctx: Any) -> None:
    """Resume/retry not available while awaiting feedback — use Start instead."""
    if task.get("reason") == "awaiting_feedback":
        raise ValueError(
            f"Task '{task['id']}' is awaiting feedback. Post instructions, then use Start."
        )


async def _require_awaiting_feedback(task: dict, **ctx: Any) -> None:
    """Start/cancel_reopen requires reason == 'awaiting_feedback'."""
    if task.get("reason") != "awaiting_feedback":
        raise ValueError(
            f"Task '{task['id']}' is not awaiting feedback (reason={task.get('reason')}). "
            f"Only tasks in 'awaiting_feedback' state can be started/cancel_reopened."
        )


_GATE_FAILURE_REASONS = frozenset({"max_test_retries", "max_review_retries", "review_stalled"})


async def _require_gate_failure_reason(task: dict, **ctx: Any) -> None:
    """Skip gate from stopped requires a gate failure reason."""
    if task.get("reason") not in _GATE_FAILURE_REASONS:
        raise ValueError(
            f"Task '{task['id']}' cannot skip gate: reason '{task.get('reason')}' is not a gate failure. "
            f"Skip gate is only available for: {', '.join(sorted(_GATE_FAILURE_REASONS))}."
        )


async def _reject_if_awaiting_feedback_close(task: dict, **ctx: Any) -> None:
    """Close on stopped should not appear when reason is awaiting_feedback (use cancel_reopen)."""
    if task.get("reason") == "awaiting_feedback":
        raise ValueError(
            f"Task '{task['id']}' is awaiting feedback — use Cancel Reopen instead of Close."
        )


async def _require_held(task: dict, **ctx: Any) -> None:
    """Approve requires the task to have held=True."""
    if not task.get("held"):
        raise ValueError(f"Task '{task['id']}' is not held")


async def _approve_post_message(task: dict, **ctx: Any) -> None:
    """Post 'Approved' status message."""
    await db.post_task_message(
        task_id=task["id"], author="dispatcher", type="status",
        title="Approved",
        content="Task hold released. Dispatching.",
    )


async def _approve_dispatch_if_ready(task: dict, **ctx: Any) -> None:
    """Dispatch if dependencies are met. Imports dispatch_task from engine for test-patch compatibility."""
    from switchboard.dispatch.engine import dispatch_task

    task_id = task["id"]
    if task.get("depends_on"):
        parent = await db.get_task(task["depends_on"])
        if parent and not parent.get("gate_passed_at"):
            return  # Parent not yet gate-passed — don't dispatch

    await dispatch_task(
        project_id=task["project_id"],
        task_id=task_id,
        goal=task["goal"],
    )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IllegalTransition(ValueError):
    """Raised when a state transition is not allowed."""

    def __init__(
        self,
        current_state: str,
        action: str,
        task_id: str | None = None,
        available: list[str] | None = None,
    ):
        self.current_state = current_state
        self.action = action
        msg = f"Cannot '{action}' from state '{current_state}'"
        if task_id:
            msg = f"Task '{task_id}': {msg}"
        if available:
            msg += f". Valid actions: {', '.join(available)}"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# TransitionDef
# ---------------------------------------------------------------------------


@dataclass
class TransitionDef:
    """Definition of a single state transition."""

    to_state: str | Callable  # static string or dynamic resolver
    reason: str | Callable | None = None
    preconditions: list[Callable] = field(default_factory=list)
    side_effects: list[Callable] = field(default_factory=list)
    label: str = ""  # button label for dashboard
    style: str = "secondary"  # primary, secondary, danger
    confirm: bool = False  # require confirmation dialog
    user_action: bool = True  # False for system-initiated transitions (not shown as dashboard buttons)

    def resolve_target(self, task: dict, **ctx: Any) -> tuple[str, str | None]:
        """Resolve the target state and reason, handling dynamic callables."""
        state = self.to_state(task, **ctx) if callable(self.to_state) else self.to_state
        reason = self.reason(task, **ctx) if callable(self.reason) else self.reason
        return state, reason


# ---------------------------------------------------------------------------
# Transition table — every valid (state, action) pair
# ---------------------------------------------------------------------------


def _exhaust_turns_state(task: dict, **ctx: Any) -> str:
    """Turns exhausted always goes to stopped — work is incomplete, needs user review."""
    return "stopped"


def _exhaust_turns_reason(task: dict, **ctx: Any) -> str | None:
    """Turns exhausted reason."""
    return "turns_exhausted"


def _gate_fail_reason(task: dict, **ctx: Any) -> str | None:
    """Reason for gate_fail comes from context (the gate sub-machine)."""
    return ctx.get("reason", "gate_failed")


OUTCOME_DEFINITIONS = {
    # Lifecycle reasons (stored in task_attempts.outcome)
    "gate_passed": {"label": "completed", "color": "#22c55e"},
    "gate_skipped": {"label": "completed", "color": "#22c55e"},
    "paused_by_user": {"label": "stopped", "color": "#6b7280"},
    "dispatch_error": {"label": "failed", "color": "#ef4444"},
    "wall_clock_timeout": {"label": "timeout", "color": "#ef4444"},
    "rate_limited": {"label": "rate-limited", "color": "#eab308"},
    "turns_exhausted": {"label": "turns exhausted", "color": "#eab308"},
    "recovery_pending": {"label": "stopped", "color": "#6b7280"},
    "recovery_failed": {"label": "failed", "color": "#ef4444"},
    # Legacy heuristic outcomes (from _determine_attempt_outcome)
    "in-progress": {"label": "in progress", "color": "#eab308"},
    "retried": {"label": "retried", "color": "#6b7280"},
    "success": {"label": "completed", "color": "#22c55e"},
    "test-failure": {"label": "tests failed", "color": "#ef4444"},
    "review-rejection": {"label": "review rejected", "color": "#ef4444"},
    "error": {"label": "failed", "color": "#ef4444"},
    "failed": {"label": "failed", "color": "#ef4444"},
    "cancelled": {"label": "cancelled", "color": "#6b7280"},
    "completed": {"label": "completed", "color": "#22c55e"},
    "max_test_retries": {"label": "tests failed", "color": "#ef4444"},
    "max_review_retries": {"label": "review rejected", "color": "#ef4444"},
    "review_stalled": {"label": "review stalled", "color": "#ef4444"},
    "gate_failed": {"label": "failed", "color": "#ef4444"},
    # Hyphenated aliases for heuristic-generated outcomes
    "wall-clock-timeout": {"label": "timeout", "color": "#ef4444"},
    "turns-exhausted": {"label": "turns exhausted", "color": "#eab308"},
    # Gate auto-retry outcomes
    "test_failure": {"label": "tests failed", "color": "#ef4444"},
    "review_rejected": {"label": "review rejected", "color": "#ef4444"},
    # Defensive entries
    "awaiting_feedback": {"label": "awaiting feedback", "color": "#eab308"},
    "manually_closed": {"label": "closed", "color": "#6b7280"},
}

_OUTCOME_FALLBACK = {"label": "unknown", "color": "#6b7280"}


def get_outcome_definition(reason: str) -> dict:
    """Return display label and color for an attempt outcome reason."""
    return OUTCOME_DEFINITIONS.get(reason, _OUTCOME_FALLBACK)


async def _reopen_attempt(task: dict, **ctx: Any) -> None:
    """Clear finished_at and outcome on the current attempt (resume/gate_retry)."""
    attempt = task.get("current_attempt")
    if not attempt:
        return
    await db.update_attempt(task["id"], attempt, finished_at=None, outcome=None)


async def _finalize_attempt(task: dict, **ctx: Any) -> None:
    """Close out the current attempt record with finished_at and outcome."""
    attempt = task.get("current_attempt")
    if not attempt:
        return
    outcome = ctx.get("outcome") or task.get("reason") or ctx.get("_previous_status", "unknown")
    await db.update_attempt(task["id"], attempt, finished_at=db.now_iso(), outcome=outcome)


TRANSITIONS: dict[tuple[str, str], TransitionDef] = {
    # --- User-Initiated Actions -------------------------------------------
    ("ready", "dispatch"): TransitionDef(
        to_state="working",
        label="Dispatch",
        style="primary",
        side_effects=[_dispatch_launch_session],
    ),
    ("ready", "approve"): TransitionDef(
        to_state="ready",  # State stays ready; _approve_dispatch_if_ready side effect may advance to working
        preconditions=[_require_held],
        side_effects=[_clear_held_flag, _approve_post_message, _approve_dispatch_if_ready],
        label="Approve",
        style="primary",
        user_action=True,
    ),
    ("ready", "cancel"): TransitionDef(
        to_state="cancelled",
        side_effects=[_revert_punchlist, _clear_held_flag, _drain_queue_effect],
        label="Discard",
        style="danger",
        confirm=True,
    ),
    ("working", "stop"): TransitionDef(
        to_state="stopped",
        reason="paused_by_user",
        side_effects=[_stop_cc_session, _stop_gate_subprocess, _post_stop_message, _drain_queue_effect, _finalize_attempt],
        label="Stop",
        style="secondary",
        confirm=False,
    ),
    ("working", "cancel"): TransitionDef(
        to_state="cancelled",
        reason="cancelled",
        side_effects=[_cancel_running_process, _revert_punchlist, _clear_held_flag, _drain_queue_effect, _finalize_attempt],
        # No label — Cancel not shown in dashboard for working state.
        # User flow: Stop first → land in stopped → then Cancel if needed.
        style="danger",
        confirm=True,
    ),
    ("validating", "stop"): TransitionDef(
        to_state="stopped",
        reason="paused_by_user",
        side_effects=[_stop_cc_session, _stop_gate_subprocess, _post_stop_message, _drain_queue_effect, _finalize_attempt],
        label="Stop",
        style="secondary",
        confirm=False,
    ),
    ("validating", "skip_gate"): TransitionDef(
        to_state="completed",
        reason="gate_skipped",
        side_effects=[_stop_gate_subprocess, _skip_gate_set_fields, _skip_gate_post_message, _skip_gate_dispatch_dependents, _finalize_attempt],
        label="Skip Gate",
        style="secondary",
        confirm=True,
    ),
    ("validating", "cancel"): TransitionDef(
        to_state="cancelled",
        reason="cancelled",
        side_effects=[_cancel_running_process, _revert_punchlist, _clear_held_flag, _drain_queue_effect, _finalize_attempt],
        # No label — Cancel not shown in dashboard for validating state.
        # User flow: Stop first → land in stopped → then Cancel if needed.
        style="danger",
        confirm=True,
    ),
    ("stopped", "resume"): TransitionDef(
        to_state="working",
        label="Resume",
        style="primary",
        preconditions=[_reject_awaiting_feedback, _require_session_or_gate_resumable],
        side_effects=[_reopen_attempt, _resume_launch_session],
    ),
    ("stopped", "retry"): TransitionDef(
        to_state="working",
        label="Retry",
        style="primary",
        preconditions=[_reject_awaiting_feedback],
        side_effects=[_retry_launch_session],
    ),
    ("stopped", "start"): TransitionDef(
        to_state="working",
        label="Start",
        style="primary",
        preconditions=[_require_awaiting_feedback],
        side_effects=[_start_launch_session],
    ),
    ("stopped", "skip_gate"): TransitionDef(
        to_state="completed",
        reason="gate_skipped",
        preconditions=[_require_gate_failure_reason],
        side_effects=[_stop_gate_subprocess, _skip_gate_set_fields, _skip_gate_post_message, _skip_gate_dispatch_dependents, _finalize_attempt],
        label="Skip Gate",
        style="secondary",
        confirm=True,
    ),
    ("stopped", "cancel"): TransitionDef(
        to_state="cancelled",
        reason="cancelled",
        preconditions=[_reject_awaiting_feedback],
        side_effects=[_revert_punchlist, _clear_held_flag, _drain_queue_effect, _finalize_attempt],
        label="Cancel",
        style="danger",
        confirm=True,
    ),
    ("stopped", "close"): TransitionDef(
        to_state="completed",
        reason="manually_closed",
        preconditions=[_reject_if_working, _reject_if_awaiting_feedback_close],
        side_effects=[_close_archive_and_cleanup, _post_close_message, _finalize_attempt],
        label="Close",
        style="secondary",
        confirm=True,
    ),
    ("completed", "retry"): TransitionDef(
        to_state="working",
        side_effects=[_retry_launch_session],
    ),
    ("completed", "reopen"): TransitionDef(
        to_state="stopped",
        reason="awaiting_feedback",
        label="Reopen",
        style="secondary",
        confirm=True,
        side_effects=[_reopen_side_effects],
    ),
    ("cancelled", "retry"): TransitionDef(
        to_state="working",
        label="Retry",
        style="primary",
        side_effects=[_retry_launch_session],
    ),
    ("cancelled", "resume"): TransitionDef(
        to_state="working",
        label="Resume",
        style="primary",
        preconditions=[_require_session_id],
        side_effects=[_reopen_attempt, _resume_launch_session],
    ),
    ("stopped", "cancel_reopen"): TransitionDef(
        to_state="completed",
        label="Cancel Reopen",
        style="secondary",
        preconditions=[_require_awaiting_feedback],
        side_effects=[_cancel_reopen_side_effects],
    ),
    # --- System-Initiated Actions -----------------------------------------
    ("working", "complete"): TransitionDef(
        to_state="validating",
        reason="completed",
        label="Complete",
        user_action=False,
        side_effects=[_on_sdk_complete, _finalize_attempt],
    ),
    ("working", "exhaust_turns"): TransitionDef(
        to_state=_exhaust_turns_state,
        reason=_exhaust_turns_reason,
        label="Exhaust Turns",
        user_action=False,
        side_effects=[_on_exhaust_turns, _finalize_attempt],
    ),
    ("working", "timeout"): TransitionDef(
        to_state="stopped",
        reason="wall_clock_timeout",
        label="Timeout",
        user_action=False,
        side_effects=[_on_timeout, _finalize_attempt],
    ),
    ("working", "rate_limit"): TransitionDef(
        to_state="stopped",
        reason="rate_limited",
        label="Rate Limit",
        user_action=False,
        side_effects=[_on_rate_limit, _finalize_attempt],
    ),
    ("working", "error"): TransitionDef(
        to_state="stopped",
        reason="dispatch_error",
        label="Error",
        user_action=False,
        side_effects=[_on_error, _finalize_attempt],
    ),
    ("validating", "gate_pass"): TransitionDef(
        to_state="completed",
        reason="gate_passed",
        label="Gate Pass",
        user_action=False,
        side_effects=[_on_gate_pass, _finalize_attempt],
    ),
    ("validating", "gate_fail"): TransitionDef(
        to_state="stopped",
        reason=_gate_fail_reason,
        label="Gate Fail",
        user_action=False,
        side_effects=[_on_gate_fail, _finalize_attempt],
    ),
    ("validating", "gate_retry"): TransitionDef(
        to_state="working",
        label="Gate Retry",
        user_action=False,
        side_effects=[_reopen_attempt],
    ),
    ("validating", "retry"): TransitionDef(
        to_state="working",
        user_action=False,
        side_effects=[_finalize_attempt, _retry_launch_session],
    ),
    ("validating", "resume"): TransitionDef(
        to_state="working",
        user_action=False,
        side_effects=[_reopen_attempt, _resume_launch_session],
    ),
    ("working", "signal_kill"): TransitionDef(
        to_state="working",
        label="Signal Kill",
        user_action=False,
        side_effects=[_on_signal_kill],
    ),
    # --- Recovery Actions -------------------------------------------------
    ("working", "recover_park"): TransitionDef(
        to_state="stopped",
        reason="recovery_pending",
        user_action=False,
        side_effects=[_drain_queue_effect, _finalize_attempt],
    ),
    ("stopped", "recover_park"): TransitionDef(
        to_state="stopped",
        reason="recovery_pending",
        user_action=False,
    ),
    ("stopped", "recover_queue"): TransitionDef(
        to_state="ready",
        user_action=False,
        side_effects=[_recover_queue_side_effects],
    ),
    ("stopped", "recover_fail"): TransitionDef(
        to_state="stopped",
        reason="recovery_failed",
        user_action=False,
        side_effects=[_recover_fail_post_message],
    ),
    ("working", "recover_fail"): TransitionDef(
        to_state="stopped",
        reason="recovery_failed",
        user_action=False,
        side_effects=[_recover_fail_post_message, _drain_queue_effect, _finalize_attempt],
    ),
    ("working", "recover_cancel"): TransitionDef(
        to_state="cancelled",
        reason="cancelled",
        user_action=False,
        side_effects=[_revert_punchlist, _finalize_attempt],
    ),
    ("stopped", "recover_cancel"): TransitionDef(
        to_state="cancelled",
        user_action=False,
        side_effects=[_revert_punchlist],
    ),
}


# ---------------------------------------------------------------------------
# Status mapping — old DB values → 6-state model
# ---------------------------------------------------------------------------

_STATUS_MAP: dict[str, str] = {
    # Old values → new states
    "pending-validation": "validating",
    "needs-review": "stopped",
    "turns-exhausted": "stopped",  # default; overridden if gates running
    "rate-limited": "stopped",
    "failed": "stopped",
    "reopened": "stopped",
    "merged": "completed",
    "blocked": "ready",
    # New values pass through
    "ready": "ready",
    "working": "working",
    "validating": "validating",
    "stopped": "stopped",
    "completed": "completed",
    "cancelled": "cancelled",
}

# Gate statuses that indicate the gate sub-machine is active
_ACTIVE_GATE_STATUSES = {"testing", "reviewing", "test-passed"}


# ---------------------------------------------------------------------------
# State labels — (state, reason) → display info for dashboard
# ---------------------------------------------------------------------------

STATE_LABELS: dict[tuple[str, str | None], dict[str, Any]] = {
    ("ready", None): {"label": "Ready", "color": "#6b7280", "pulse": False},
    ("ready", "held"): {"label": "Held", "color": "#f59e0b", "pulse": False},
    ("ready", "queued"): {"label": "Queued", "color": "#6b7280", "pulse": False},
    ("ready", "blocked"): {"label": "Blocked", "color": "#f59e0b", "pulse": False},
    ("working", None): {"label": "Working", "color": "#3b82f6", "pulse": True},
    ("validating", "testing"): {"label": "Testing", "color": "#8b5cf6", "pulse": True},
    ("validating", "reviewing"): {"label": "Reviewing", "color": "#8b5cf6", "pulse": True},
    ("validating", "pushing"): {"label": "Pushing", "color": "#8b5cf6", "pulse": True},
    ("validating", None): {"label": "Validating", "color": "#8b5cf6", "pulse": True},
    ("stopped", "paused_by_user"): {"label": "Paused", "color": "#f59e0b", "pulse": False},
    ("stopped", "turns_exhausted"): {"label": "Turns Exhausted", "color": "#f59e0b", "pulse": False},
    ("stopped", "wall_clock_timeout"): {"label": "Timed Out", "color": "#f59e0b", "pulse": False},
    ("stopped", "rate_limited"): {"label": "Rate Limited", "color": "#f59e0b", "pulse": False},
    ("stopped", "max_test_retries"): {"label": "Tests Failed", "color": "#ef4444", "pulse": False},
    ("stopped", "max_review_retries"): {"label": "Review Failed", "color": "#ef4444", "pulse": False},
    ("stopped", "review_stalled"): {"label": "Review Stalled", "color": "#ef4444", "pulse": False},
    ("stopped", "dispatch_error"): {"label": "Error", "color": "#ef4444", "pulse": False},
    ("stopped", "worktree_missing"): {"label": "Worktree Missing", "color": "#ef4444", "pulse": False},
    ("stopped", "push_failed"): {"label": "Push Failed", "color": "#ef4444", "pulse": False},
    ("stopped", "awaiting_feedback"): {"label": "Awaiting Feedback", "color": "#f59e0b", "pulse": False},
    ("stopped", "recovery_pending"): {"label": "Recovering", "color": "#f59e0b", "pulse": True},
    ("stopped", "recovery_failed"): {"label": "Recovery Failed", "color": "#ef4444", "pulse": False},
    ("stopped", "recovery_limit"): {"label": "Recovery Failed", "color": "#ef4444", "pulse": False},
    ("stopped", None): {"label": "Stopped", "color": "#f59e0b", "pulse": False},
    ("completed", "gate_passed"): {"label": "Completed", "color": "#10b981", "pulse": False},
    ("completed", "gate_skipped"): {"label": "Completed (Skipped)", "color": "#10b981", "pulse": False},
    ("completed", "manually_closed"): {"label": "Closed", "color": "#10b981", "pulse": False},
    ("completed", None): {"label": "Completed", "color": "#10b981", "pulse": False},
    ("cancelled", None): {"label": "Cancelled", "color": "#6b7280", "pulse": False},
}

# Fallback labels by state only (when no specific reason match)
_STATE_FALLBACKS: dict[str, dict[str, Any]] = {
    "ready": {"label": "Ready", "color": "#6b7280", "pulse": False},
    "working": {"label": "Working", "color": "#3b82f6", "pulse": True},
    "validating": {"label": "Validating", "color": "#8b5cf6", "pulse": True},
    "stopped": {"label": "Stopped", "color": "#f59e0b", "pulse": False},
    "completed": {"label": "Completed", "color": "#10b981", "pulse": False},
    "cancelled": {"label": "Cancelled", "color": "#6b7280", "pulse": False},
}


# ---------------------------------------------------------------------------
# Helpers for derived state
# ---------------------------------------------------------------------------


def _effective_ready_reason(task: dict) -> str | None:
    """Derive the display reason for a ready-state task.

    The raw DB status can be "blocked" (depends_on not met), or the task
    can have held=True or queued_at set. None means dispatchable.
    """
    if task.get("status") == "blocked":
        return "blocked"
    if task.get("held"):
        return "held"
    if task.get("queued_at"):
        return "queued"
    return None


async def _determine_queued_reason(task: dict) -> tuple[str, str | None]:
    """Return (queued_reason, blocking_task_id) for a queued task.

    Priority order:
    1. dependency — depends_on is set and parent hasn't gate-passed
    2. project_paused — the task's project is paused
    3. component_paused — the task's component is paused
    4. concurrency — fallback: waiting for a dispatch slot
    """
    depends_on = task.get("depends_on")
    if depends_on:
        parent = await db.get_task(depends_on)
        if parent and not parent.get("gate_passed_at"):
            return "dependency", depends_on

    project = await db.get_project(task["project_id"])
    if project and project.get("paused"):
        return "project_paused", None

    component_id = task.get("component_id")
    if component_id:
        component = await db.get_component(component_id)
        if component and component.get("paused"):
            return "component_paused", None

    return "concurrency", None


# ---------------------------------------------------------------------------
# TaskLifecycle service
# ---------------------------------------------------------------------------


class TaskLifecycle:
    """Single owner of all task state transitions.

    All state changes go through execute(). Nothing else should call
    db.update_task(status=...) directly once migration is complete.
    """

    async def execute(self, task_id: str, action: str, **context: Any) -> dict:
        """Execute a state transition.

        Args:
            task_id: The task to transition.
            action: The action to perform (e.g. "dispatch", "stop", "gate_pass").
            **context: Additional context passed to dynamic resolvers,
                       preconditions, and side effects.

        Returns:
            The updated task dict.

        Raises:
            ValueError: If task not found.
            IllegalTransition: If the transition is not valid.
        """
        # 1. Read task from DB
        task = await db.get_task(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")

        # 2. Map to effective state
        effective = self._effective_state(task)

        # 3. Look up transition
        key = (effective, action)
        tdef = TRANSITIONS.get(key)

        if tdef is None:
            # Collect available actions for error message
            available = [
                a for (s, a) in TRANSITIONS if s == effective
            ]
            raise IllegalTransition(
                current_state=effective,
                action=action,
                task_id=task_id,
                available=available,
            )

        # 4. Run preconditions (each can raise)
        for precond in tdef.preconditions:
            await precond(task, **context)

        # 5. Resolve target state and reason
        new_state, reason = tdef.resolve_target(task, **context)

        # 6. Update DB
        update_fields: dict[str, Any] = {"status": new_state}
        if reason is not None:
            update_fields["reason"] = reason
        elif new_state != task.get("status"):
            # Clear reason when transitioning to a new state without explicit reason
            update_fields["reason"] = None

        previous_status = task["status"]
        updated_task = await db.update_task(task_id, **update_fields)

        # 7. Write audit log
        await write_audit_log(
            task_id=task_id,
            action=action,
            triggered_by=context.get("triggered_by", "lifecycle"),
            source_detail=context.get("source_detail"),
            previous_status=previous_status,
            new_status=new_state,
        )

        logger.info(
            "Task %s: %s -> %s (action=%s, reason=%s)",
            task_id, effective, new_state, action, reason,
        )

        # 8. Fire side effects (non-blocking errors logged, not raised)
        context["_previous_status"] = previous_status
        # Expose pre-transition gate state for reopen side effects
        context["_saved_gate_status"] = task.get("gate_status")
        context["_saved_gate_passed_at"] = task.get("gate_passed_at")
        for effect in tdef.side_effects:
            try:
                await effect(updated_task, **context)
            except Exception:
                logger.exception(
                    "Side effect failed for task %s action %s", task_id, action,
                )

        # 9. Re-read task in case side effects changed status (e.g. retry launch failure → needs-review)
        final_task = await db.get_task(task_id)
        return final_task or updated_task

    async def get_available_actions(self, task_id: str) -> list[dict]:
        """Return valid user-facing actions for the task's current state.

        Evaluates preconditions to filter reason-specific actions.
        Dashboard uses this to render action buttons.
        """
        task = await db.get_task(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")

        effective = self._effective_state(task)

        # Handle ready sub-states: held / queued / blocked / dispatchable
        if effective == "ready":
            cancel_def = TRANSITIONS[("ready", "cancel")]
            cancel_action = {
                "name": "cancel",
                "label": cancel_def.label,
                "style": cancel_def.style,
                "confirm": cancel_def.confirm,
            }
            if task.get("held"):
                return [
                    {"name": "approve", "label": "Approve", "style": "primary", "confirm": False},
                    cancel_action,
                ]
            if task.get("queued_at") or task.get("status") == "blocked":
                return [cancel_action]
            # Dispatchable — use transition table (dispatch + cancel)

        actions = []
        for (state, action), tdef in TRANSITIONS.items():
            if state != effective:
                continue
            if not tdef.label or not tdef.user_action:
                continue
            # Evaluate preconditions — exclude action if any precondition fails
            precond_passed = True
            for precond in tdef.preconditions:
                try:
                    await precond(task)
                except ValueError:
                    precond_passed = False
                    break
            if precond_passed:
                actions.append({
                    "name": action,
                    "label": tdef.label,
                    "style": tdef.style,
                    "confirm": tdef.confirm,
                })

        # For stopped state, combine cancel + close into a single compound end_task action
        if effective == "stopped":
            action_names = {a["name"] for a in actions}
            if "cancel" in action_names and "close" in action_names:
                actions = [a for a in actions if a["name"] not in ("cancel", "close")]
                actions.append({
                    "name": "end_task",
                    "label": "End Task",
                    "style": "compound",
                    "confirm": None,
                    "options": [
                        {"action": "close", "label": "Complete", "description": "Mark as done. Work and branch preserved."},
                        {"action": "cancel", "label": "Discard", "description": "Mark as unwanted. Removed from active view."},
                    ],
                })

        return actions

    async def get_state_label(self, task_id: str) -> dict:
        """Return user-facing label, color, and pulse for dashboard display."""
        task = await db.get_task(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")

        effective = self._effective_state(task)
        # For ready state, reason is derived from task fields (not stored in DB)
        if effective == "ready":
            reason = _effective_ready_reason(task)
        else:
            reason = task.get("reason")

        # Try exact (state, reason) match first
        info = STATE_LABELS.get((effective, reason))
        if info is None:
            # Fall back to (state, None) then state-level fallback
            info = STATE_LABELS.get((effective, None))
        if info is None:
            info = _STATE_FALLBACKS.get(effective, {
                "label": effective.title(),
                "color": "#6b7280",
                "pulse": False,
            })

        queued_reason = None
        queued_blocking_task_id = None
        if reason == "queued":
            queued_reason, queued_blocking_task_id = await _determine_queued_reason(task)

        return {
            "state": effective,
            "reason": reason,
            "label": info["label"],
            "color": info["color"],
            "pulse": info["pulse"],
            "queued_reason": queued_reason,
            "queued_blocking_task_id": queued_blocking_task_id,
        }

    def _effective_state(self, task: dict) -> str:
        """Map raw DB status to the 6-state model.

        Handles old status values during the migration period.
        Special case: turns-exhausted with active gate_status maps to
        validating instead of stopped.
        """
        raw_status = task["status"]

        # Special case: turns-exhausted with active gates → validating
        if raw_status == "turns-exhausted":
            gate_status = task.get("gate_status")
            if gate_status in _ACTIVE_GATE_STATUSES:
                return "validating"
            return "stopped"

        mapped = _STATUS_MAP.get(raw_status)
        if mapped is not None:
            return mapped

        # Unknown status — pass through (defensive)
        logger.warning("Unknown task status '%s', passing through", raw_status)
        return raw_status


# Module-level singleton
lifecycle = TaskLifecycle()
