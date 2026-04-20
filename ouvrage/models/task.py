"""Task dataclass, TaskStatus enum, and GateStatus enum."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TaskStatus(str, Enum):
    """All task status values, including display-layer states.

    The lifecycle state machine in ``ouvrage/dispatch/lifecycle.py`` operates on
    six core DB states: ``ready``, ``working``, ``validating``, ``stopped``,
    ``completed``, ``cancelled``. The other values here (``BLOCKED``, ``TESTING``,
    ``REVIEWING``, ``NEEDS_REVIEW``, ``TURNS_EXHAUSTED``, ``REOPENED``,
    ``MERGED``, ``FAILED``) are display-layer states computed at read time by
    ``_effective_state()`` / ``_effective_ready_reason()`` for the dashboard.

    The authoritative mapping from DB state + task flags → display state lives
    in ``ouvrage/config/constants.py::CORE_STATE_DEFINITIONS``. The transition
    table is ``ouvrage/dispatch/lifecycle.py::TRANSITIONS``. Every status
    change must go through ``TaskLifecycle.execute()``.
    """
    READY = "ready"
    BLOCKED = "blocked"
    WORKING = "working"
    TESTING = "testing"
    REVIEWING = "reviewing"
    NEEDS_REVIEW = "needs-review"
    TURNS_EXHAUSTED = "turns-exhausted"
    REOPENED = "reopened"
    COMPLETED = "completed"
    MERGED = "merged"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GateStatus(str, Enum):
    """Status of a test or review gate."""
    NOT_RUN = "not-run"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Task:
    """Typed representation of a row from the tasks table."""

    # Required fields
    id: str
    project_id: str
    goal: str
    status: str = TaskStatus.READY.value

    # Branch and worktree
    phase: Optional[str] = None
    branch: Optional[str] = None
    worktree_path: Optional[str] = None
    base_branch: Optional[str] = None
    branch_target: Optional[str] = None

    # Session / process
    session_id: Optional[str] = None
    pid: Optional[int] = None

    # Resource limits
    max_turns: Optional[int] = None
    max_wall_clock: Optional[int] = None

    # Token / cost tracking
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    dispatch_count: int = 0

    # Timestamps
    last_activity: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    queued_at: Optional[str] = None
    pushed_at: Optional[str] = None
    retry_after: Optional[str] = None

    # Linked entities
    jira_ticket: Optional[str] = None
    conversation_id: Optional[str] = None
    component_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    depends_on: Optional[str] = None

    # Model selection
    model: Optional[str] = None
    review_model: str = "opus"

    # Gate configuration
    auto_test: bool = True
    gate_status: Optional[str] = None
    gate_retries: int = 0
    max_gate_retries: int = 3
    gate_passed_at: Optional[str] = None
    max_test_retries: Optional[int] = None
    max_review_retries: Optional[int] = None

    # PR automation
    auto_review: bool = True
    auto_pr: bool = False
    auto_merge: bool = False
    pr_status: Optional[str] = None
    pr_error: Optional[str] = None

    # Worktree cleanup
    auto_release_worktree: bool = True

    # External links
    claude_chat_url: Optional[str] = None

    # Recovery / flap detection
    recovery_count: int = 0
    last_recovery_at: Optional[str] = None
    recovery_priority: bool = False

    # Gate result storage
    last_test_output: Optional[str] = None  # JSON string

    # Dispatch tracking
    current_attempt: int = 1

    # Hold for approval
    held: bool = False

    # Reopen state
    reopen_saved_gate_status: Optional[str] = None
    reopen_saved_gate_passed_at: Optional[str] = None
