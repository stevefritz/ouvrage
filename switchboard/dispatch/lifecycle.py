"""TaskLifecycle service — owns ALL task state transitions.

Single entry point for state changes. Contains the transition table,
effective state mapper, state labels, and the execute() method.

cancel, close, and skip_gate are routed through this service.
"""

from __future__ import annotations

import logging
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

    cleanup = ctx.get("cleanup", True)
    force_delete_branch = ctx.get("force_delete_branch", False)

    update_fields: dict[str, Any] = {"gate_passed_at": None, "held": False}
    if cleanup and project:
        await cleanup_worktree(project, task, force_delete_branch)
        update_fields["worktree_path"] = None

    await db.update_task(task["id"], **update_fields)


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
    await db.post_task_message(
        task_id=task["id"], author="dispatcher", type="status",
        title="Gate skipped",
        content="Gate manually bypassed by user.",
    )


async def _skip_gate_dispatch_dependents(task: dict, **ctx: Any) -> None:
    """Trigger chain advancement after gate skip."""
    from switchboard.dispatch.engine import _check_and_dispatch_dependents
    await _check_and_dispatch_dependents(task["id"])


# ---------------------------------------------------------------------------
# Precondition functions
# ---------------------------------------------------------------------------


async def _reject_if_working(task: dict, **ctx: Any) -> None:
    """Precondition: reject close if task is still working."""
    if task["status"] == "working":
        raise ValueError(
            f"Task '{task['id']}' is still running. Cancel it first, then close."
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

    def resolve_target(self, task: dict, **ctx: Any) -> tuple[str, str | None]:
        """Resolve the target state and reason, handling dynamic callables."""
        state = self.to_state(task, **ctx) if callable(self.to_state) else self.to_state
        reason = self.reason(task, **ctx) if callable(self.reason) else self.reason
        return state, reason


# ---------------------------------------------------------------------------
# Transition table — every valid (state, action) pair
# ---------------------------------------------------------------------------


def _exhaust_turns_state(task: dict, **ctx: Any) -> str:
    """Dynamic target for exhaust_turns: validating if gates configured, else stopped."""
    project = ctx.get("project")
    if project and project.get("test_command"):
        return "validating"
    return "stopped"


def _exhaust_turns_reason(task: dict, **ctx: Any) -> str | None:
    """Dynamic reason for exhaust_turns."""
    project = ctx.get("project")
    if project and project.get("test_command"):
        return None  # validating has no reason yet — gate sub-machine sets it
    return "turns_exhausted"


def _gate_fail_reason(task: dict, **ctx: Any) -> str | None:
    """Reason for gate_fail comes from context (the gate sub-machine)."""
    return ctx.get("reason", "gate_failed")


TRANSITIONS: dict[tuple[str, str], TransitionDef] = {
    # --- User-Initiated Actions -------------------------------------------
    ("ready", "dispatch"): TransitionDef(
        to_state="working",
        label="Dispatch",
        style="primary",
    ),
    ("ready", "cancel"): TransitionDef(
        to_state="cancelled",
        side_effects=[_revert_punchlist, _clear_held_flag, _drain_queue_effect],
        label="Cancel",
        style="danger",
        confirm=True,
    ),
    ("working", "stop"): TransitionDef(
        to_state="stopped",
        reason="paused_by_user",
        label="Stop",
        style="danger",
        confirm=True,
    ),
    ("working", "cancel"): TransitionDef(
        to_state="cancelled",
        side_effects=[_cancel_running_process, _revert_punchlist, _clear_held_flag, _drain_queue_effect],
        label="Cancel",
        style="danger",
        confirm=True,
    ),
    ("validating", "stop"): TransitionDef(
        to_state="stopped",
        reason="paused_by_user",
        label="Stop",
        style="danger",
        confirm=True,
    ),
    ("validating", "skip_gate"): TransitionDef(
        to_state="completed",
        reason="gate_skipped",
        side_effects=[_skip_gate_set_fields, _skip_gate_post_message, _skip_gate_dispatch_dependents],
        label="Skip Gate",
        style="secondary",
        confirm=True,
    ),
    ("validating", "cancel"): TransitionDef(
        to_state="cancelled",
        side_effects=[_cancel_running_process, _revert_punchlist, _clear_held_flag, _drain_queue_effect],
        label="Cancel",
        style="danger",
        confirm=True,
    ),
    ("stopped", "resume"): TransitionDef(
        to_state="working",
        label="Resume",
        style="primary",
    ),
    ("stopped", "retry"): TransitionDef(
        to_state="working",
        label="Retry",
        style="primary",
    ),
    ("stopped", "start"): TransitionDef(
        to_state="working",
        label="Start",
        style="primary",
    ),
    ("stopped", "skip_gate"): TransitionDef(
        to_state="completed",
        reason="gate_skipped",
        side_effects=[_skip_gate_set_fields, _skip_gate_post_message, _skip_gate_dispatch_dependents],
        label="Skip Gate",
        style="secondary",
        confirm=True,
    ),
    ("stopped", "cancel"): TransitionDef(
        to_state="cancelled",
        side_effects=[_revert_punchlist, _clear_held_flag, _drain_queue_effect],
        label="Cancel",
        style="danger",
        confirm=True,
    ),
    ("stopped", "close"): TransitionDef(
        to_state="completed",
        reason="manually_closed",
        preconditions=[_reject_if_working],
        side_effects=[_close_archive_and_cleanup, _post_close_message],
        label="Close",
        style="secondary",
        confirm=True,
    ),
    ("completed", "reopen"): TransitionDef(
        to_state="stopped",
        reason="awaiting_feedback",
        label="Reopen",
        style="secondary",
        confirm=True,
    ),
    ("cancelled", "retry"): TransitionDef(
        to_state="working",
        label="Retry",
        style="primary",
    ),
    ("cancelled", "resume"): TransitionDef(
        to_state="working",
        label="Resume",
        style="primary",
    ),
    # --- System-Initiated Actions -----------------------------------------
    ("working", "complete"): TransitionDef(
        to_state="validating",
        label="Complete",
    ),
    ("working", "exhaust_turns"): TransitionDef(
        to_state=_exhaust_turns_state,
        reason=_exhaust_turns_reason,
        label="Exhaust Turns",
    ),
    ("working", "timeout"): TransitionDef(
        to_state="stopped",
        reason="wall_clock_timeout",
        label="Timeout",
    ),
    ("working", "rate_limit"): TransitionDef(
        to_state="stopped",
        reason="rate_limited",
        label="Rate Limit",
    ),
    ("working", "error"): TransitionDef(
        to_state="stopped",
        reason="dispatch_error",
        label="Error",
    ),
    ("validating", "gate_pass"): TransitionDef(
        to_state="completed",
        reason="gate_passed",
        label="Gate Pass",
    ),
    ("validating", "gate_fail"): TransitionDef(
        to_state="stopped",
        reason=_gate_fail_reason,
        label="Gate Fail",
    ),
    ("validating", "gate_retry"): TransitionDef(
        to_state="working",
        label="Gate Retry",
    ),
    ("working", "signal_kill"): TransitionDef(
        to_state="working",
        label="Signal Kill",
    ),
    # --- Recovery Actions -------------------------------------------------
    ("working", "recover"): TransitionDef(
        to_state="working",
        label="Recover",
    ),
    ("stopped", "recover"): TransitionDef(
        to_state="working",
        label="Recover",
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
        for effect in tdef.side_effects:
            try:
                await effect(updated_task, **context)
            except Exception:
                logger.exception(
                    "Side effect failed for task %s action %s", task_id, action,
                )

        # 9. Return updated task
        return updated_task

    async def get_available_actions(self, task_id: str) -> list[dict]:
        """Return valid actions for the task's current state.

        Dashboard uses this to render action buttons.
        """
        task = await db.get_task(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")

        effective = self._effective_state(task)
        actions = []
        for (state, action), tdef in TRANSITIONS.items():
            if state == effective and tdef.label:
                actions.append({
                    "name": action,
                    "label": tdef.label,
                    "style": tdef.style,
                    "confirm": tdef.confirm,
                })
        return actions

    async def get_state_label(self, task_id: str) -> dict:
        """Return user-facing label, color, and pulse for dashboard display."""
        task = await db.get_task(task_id)
        if not task:
            raise ValueError(f"Task '{task_id}' not found")

        effective = self._effective_state(task)
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

        return {
            "state": effective,
            "reason": reason,
            "label": info["label"],
            "color": info["color"],
            "pulse": info["pulse"],
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
