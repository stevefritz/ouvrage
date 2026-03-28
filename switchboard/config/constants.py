"""Named constants and default values for Switchboard."""

# ---------------------------------------------------------------------------
# Task state definitions — hardcoded defaults for the dashboard
# ---------------------------------------------------------------------------

CORE_STATE_DEFINITIONS = {
    "ready":         {"color": "#6b7280", "label": "Ready",        "pulse": False},
    "blocked":       {"color": "#f59e0b", "label": "Blocked",      "pulse": False},
    "working":       {"color": "#3b82f6", "label": "Working",      "pulse": True},
    "testing":       {"color": "#8b5cf6", "label": "Testing",      "pulse": True},
    "reviewing":     {"color": "#8b5cf6", "label": "Reviewing",    "pulse": True},
    "needs-review":  {"color": "#f59e0b", "label": "Needs Review", "pulse": False},
    "turns-exhausted": {"color": "#f59e0b", "label": "Turns Exhausted", "pulse": False},
    "reopened":      {"color": "#f59e0b", "label": "Reopened",     "pulse": False},
    "completed":     {"color": "#10b981", "label": "Completed",    "pulse": False},
    "merged":        {"color": "#10b981", "label": "Merged",       "pulse": False},
    "failed":        {"color": "#ef4444", "label": "Failed",       "pulse": False},
    "cancelled":     {"color": "#6b7280", "label": "Cancelled",    "pulse": False},
}

# ---------------------------------------------------------------------------
# Task resource limit defaults
# ---------------------------------------------------------------------------

DEFAULT_MAX_TURNS = 200
DEFAULT_MAX_WALL_CLOCK = 60  # minutes
DEFAULT_MAX_CONCURRENT = 6

# ---------------------------------------------------------------------------
# Task / component field sets (used for SQL update allowlists)
# ---------------------------------------------------------------------------

TASK_MUTABLE_FIELDS = {
    "status", "phase", "branch", "worktree_path", "session_id", "pid",
    "max_turns", "max_wall_clock",
    "total_input_tokens", "total_output_tokens", "total_cost_usd",
    "dispatch_count", "last_activity", "updated_at",
    "jira_ticket", "conversation_id",
    "auto_test", "gate_status", "gate_retries", "max_gate_retries", "gate_passed_at",
    "depends_on", "auto_review", "review_model", "parent_task_id", "auto_pr",
    "component_id", "model", "claude_chat_url",
    # v5 migration toolkit fields
    "base_branch", "branch_target",
    "max_test_retries", "max_review_retries",
    # v5 auto-merge-queue fields
    "queued_at", "auto_merge", "auto_release_worktree",
    "pushed_at", "pr_status", "pr_error",
    # v5 crash-recovery fields
    "recovery_count", "last_recovery_at", "recovery_priority",
    # v5 realtime-output fields
    "last_test_output", "current_attempt",
    # retry scheduling
    "retry_after",
    # hold/approval
    "held",
    # reopen gate state save/restore
    "reopen_saved_gate_status", "reopen_saved_gate_passed_at",
}

COMPONENT_CONFIG_FIELDS = {
    "base_branch", "setup_command", "test_command", "model",
    "auto_test", "auto_review", "review_model",
    "max_test_retries", "max_review_retries",
    "auto_pr", "auto_merge", "max_turns", "max_wall_clock",
}

COMPONENT_MUTABLE_FIELDS = COMPONENT_CONFIG_FIELDS | {
    "name", "description", "phase", "env_overrides", "secrets",
}

SYSTEM_DEFAULTS = {
    "auto_test": True,
    "auto_review": True,
    "review_model": "opus",
    "max_test_retries": 3,
    "max_review_retries": 2,
    "auto_pr": False,
    "auto_merge": False,
    "auto_release_worktree": True,
}

# ---------------------------------------------------------------------------
# Task execution
# ---------------------------------------------------------------------------

MESSAGE_POLL_INTERVAL = 5  # seconds between DB polls for injected messages
DEFAULT_MODEL = "sonnet"

# ---------------------------------------------------------------------------
# Stall detection
# ---------------------------------------------------------------------------

STALL_THRESHOLD_SECONDS = 300  # 5 minutes
STALL_CHECK_INTERVAL = 60  # check every minute

# ---------------------------------------------------------------------------
# Review constants
# ---------------------------------------------------------------------------

_DEFAULT_REVIEW_IGNORE_PATTERNS = [
    ".switchboard/",
    ".lock",
    "package-lock.json",
    "composer.lock",
    ".gitignore",
]

_TAG_REVIEW_GUIDANCE = {
    "backend": (
        "Focus on: error handling and edge cases, test coverage for failure paths, "
        "security (input validation, SQL injection, auth checks), API contract correctness."
    ),
    "frontend": (
        "Focus on: UX and user-facing correctness, accessibility (ARIA, keyboard nav), "
        "responsive behavior across screen sizes, render performance."
    ),
    "testing": (
        "Focus on: test quality and assertion correctness (assertions match spec, not just code output), "
        "coverage of edge cases and failure modes, test isolation and fixture design."
    ),
}

_DEFAULT_REVIEW_GUIDANCE = (
    "Balanced review: correctness vs spec, test quality, edge cases, code clarity."
)
