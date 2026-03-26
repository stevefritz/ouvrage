"""Backward-compatible shim — notifications moved to switchboard.notifications.slack."""
from switchboard.notifications.slack import *  # noqa: F401, F403
from switchboard.notifications.slack import (  # noqa: F401
    is_enabled,
    task_dispatched,
    task_progress,
    task_phase_changed,
    task_heartbeat,
    checklist_progress,
    task_question,
    task_completed,
    task_failed,
    task_attempt_starting,
    task_needs_review,
)
