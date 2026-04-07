"""Tests for the TaskLifecycle service.

Uses real in-memory SQLite DB via the `db` fixture. Tests state transitions
through the service interface — no mocking of db.update_task.
"""

import asyncio

import pytest

from switchboard.dispatch.lifecycle import (
    IllegalTransition,
    TaskLifecycle,
    TransitionDef,
    TRANSITIONS,
    STATE_LABELS,
    _STATUS_MAP,
    _STATE_FALLBACKS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ID = "lifecycle-test-proj"
TASK_ID = "lifecycle-test-proj/task-1"


async def _seed(db, status="ready", gate_status=None, reason=None):
    """Create a project + task at the given status."""
    try:
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )
    except Exception:
        pass  # already exists

    task = await db.create_task(
        id=TASK_ID, project_id=PROJECT_ID, goal="test lifecycle",
    )
    updates = {"status": status}
    if gate_status is not None:
        updates["gate_status"] = gate_status
    if reason is not None:
        updates["reason"] = reason
    if status != "ready":
        task = await db.update_task(TASK_ID, **updates)
    elif gate_status or reason:
        task = await db.update_task(TASK_ID, **updates)
    return task


# ---------------------------------------------------------------------------
# TransitionDef unit tests
# ---------------------------------------------------------------------------

class TestTransitionDef:
    def test_static_resolve(self):
        td = TransitionDef(to_state="working", reason="test_reason")
        state, reason = td.resolve_target({})
        assert state == "working"
        assert reason == "test_reason"

    def test_callable_resolve(self):
        td = TransitionDef(
            to_state=lambda task, **ctx: ctx.get("target", "default"),
            reason=lambda task, **ctx: ctx.get("why"),
        )
        state, reason = td.resolve_target({}, target="stopped", why="broken")
        assert state == "stopped"
        assert reason == "broken"

    def test_none_reason(self):
        td = TransitionDef(to_state="working")
        state, reason = td.resolve_target({})
        assert state == "working"
        assert reason is None

    def test_defaults(self):
        td = TransitionDef(to_state="working")
        assert td.preconditions == []
        assert td.side_effects == []
        assert td.label == ""
        assert td.style == "secondary"
        assert td.confirm is False


# ---------------------------------------------------------------------------
# IllegalTransition tests
# ---------------------------------------------------------------------------

class TestIllegalTransition:
    def test_basic_message(self):
        err = IllegalTransition("ready", "resume")
        assert "Cannot 'resume' from state 'ready'" in str(err)
        assert err.current_state == "ready"
        assert err.action == "resume"

    def test_with_task_id(self):
        err = IllegalTransition("ready", "resume", task_id="proj/task-1")
        assert "Task 'proj/task-1'" in str(err)

    def test_with_available_actions(self):
        err = IllegalTransition("ready", "resume", available=["dispatch", "cancel"])
        assert "Valid actions: dispatch, cancel" in str(err)

    def test_is_value_error(self):
        assert issubclass(IllegalTransition, ValueError)


# ---------------------------------------------------------------------------
# _effective_state tests
# ---------------------------------------------------------------------------

class TestEffectiveState:
    def setup_method(self):
        self.lifecycle = TaskLifecycle()

    def test_new_values_pass_through(self):
        for state in ("ready", "working", "validating", "stopped", "completed", "cancelled"):
            task = {"status": state}
            assert self.lifecycle._effective_state(task) == state

    def test_pending_validation_maps_to_validating(self):
        assert self.lifecycle._effective_state({"status": "pending-validation"}) == "validating"

    def test_needs_review_maps_to_stopped(self):
        assert self.lifecycle._effective_state({"status": "needs-review"}) == "stopped"

    def test_rate_limited_maps_to_stopped(self):
        assert self.lifecycle._effective_state({"status": "rate-limited"}) == "stopped"

    def test_failed_maps_to_stopped(self):
        assert self.lifecycle._effective_state({"status": "failed"}) == "stopped"

    def test_reopened_maps_to_stopped(self):
        assert self.lifecycle._effective_state({"status": "reopened"}) == "stopped"

    def test_merged_maps_to_completed(self):
        assert self.lifecycle._effective_state({"status": "merged"}) == "completed"

    def test_blocked_maps_to_ready(self):
        assert self.lifecycle._effective_state({"status": "blocked"}) == "ready"

    def test_turns_exhausted_no_gates_maps_to_stopped(self):
        task = {"status": "turns-exhausted", "gate_status": None}
        assert self.lifecycle._effective_state(task) == "stopped"

    def test_turns_exhausted_with_testing_maps_to_validating(self):
        task = {"status": "turns-exhausted", "gate_status": "testing"}
        assert self.lifecycle._effective_state(task) == "validating"

    def test_turns_exhausted_with_reviewing_maps_to_validating(self):
        task = {"status": "turns-exhausted", "gate_status": "reviewing"}
        assert self.lifecycle._effective_state(task) == "validating"

    def test_turns_exhausted_with_test_passed_maps_to_validating(self):
        task = {"status": "turns-exhausted", "gate_status": "test-passed"}
        assert self.lifecycle._effective_state(task) == "validating"

    def test_turns_exhausted_with_passed_maps_to_stopped(self):
        """passed means gates are done — not active, so maps to stopped."""
        task = {"status": "turns-exhausted", "gate_status": "passed"}
        assert self.lifecycle._effective_state(task) == "stopped"

    def test_unknown_status_passes_through(self):
        task = {"status": "some-future-status"}
        assert self.lifecycle._effective_state(task) == "some-future-status"


# ---------------------------------------------------------------------------
# execute() — valid transitions
# ---------------------------------------------------------------------------

class TestExecuteValidTransitions:
    """Test every transition in the table via execute() with real DB."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def _make_task(self, task_id, status="ready", gate_status=None, reason=None, **extra):
        task = await self.db.create_task(
            id=task_id, project_id=PROJECT_ID, goal="test",
        )
        updates = {}
        if status != "ready":
            updates["status"] = status
        if gate_status is not None:
            updates["gate_status"] = gate_status
        if reason is not None:
            updates["reason"] = reason
        updates.update(extra)
        if updates:
            task = await self.db.update_task(task_id, **updates)
        return task

    # --- User actions ---

    async def test_ready_dispatch(self):
        await self._make_task("t/1")
        result = await self.lifecycle.execute("t/1", "dispatch")
        assert result["status"] == "working"

    async def test_ready_cancel(self):
        await self._make_task("t/2")
        result = await self.lifecycle.execute("t/2", "cancel")
        assert result["status"] == "cancelled"

    async def test_working_stop(self):
        await self._make_task("t/3", status="working")
        result = await self.lifecycle.execute("t/3", "stop")
        assert result["status"] == "stopped"
        assert result["reason"] == "paused_by_user"

    async def test_working_cancel(self):
        await self._make_task("t/4", status="working")
        result = await self.lifecycle.execute("t/4", "cancel")
        assert result["status"] == "cancelled"

    async def test_validating_stop(self):
        await self._make_task("t/5", status="validating")
        result = await self.lifecycle.execute("t/5", "stop")
        assert result["status"] == "stopped"
        assert result["reason"] == "paused_by_user"

    async def test_validating_skip_gate(self):
        await self._make_task("t/6", status="validating")
        result = await self.lifecycle.execute("t/6", "skip_gate")
        assert result["status"] == "completed"
        assert result["reason"] == "gate_skipped"

    async def test_validating_cancel(self):
        await self._make_task("t/7", status="validating")
        result = await self.lifecycle.execute("t/7", "cancel")
        assert result["status"] == "cancelled"

    async def test_stopped_resume(self):
        await self._make_task("t/8", status="stopped", session_id="ses-123")
        result = await self.lifecycle.execute("t/8", "resume")
        assert result["status"] == "working"

    async def test_stopped_retry(self):
        await self._make_task("t/9", status="stopped")
        result = await self.lifecycle.execute("t/9", "retry")
        assert result["status"] == "working"

    async def test_stopped_start(self):
        await self._make_task("t/10", status="stopped", reason="awaiting_feedback")
        result = await self.lifecycle.execute("t/10", "start")
        assert result["status"] == "working"

    async def test_stopped_skip_gate(self):
        # skip_gate from stopped requires a gate failure reason
        await self._make_task("t/11", status="stopped")
        await self.db.update_task("t/11", reason="max_test_retries")
        result = await self.lifecycle.execute("t/11", "skip_gate")
        assert result["status"] == "completed"
        assert result["reason"] == "gate_skipped"

    async def test_stopped_cancel(self):
        await self._make_task("t/12", status="stopped")
        result = await self.lifecycle.execute("t/12", "cancel")
        assert result["status"] == "cancelled"

    async def test_stopped_close(self):
        await self._make_task("t/13", status="stopped")
        result = await self.lifecycle.execute("t/13", "close")
        assert result["status"] == "completed"
        assert result["reason"] == "manually_closed"

    async def test_completed_reopen(self):
        await self._make_task("t/14", status="completed")
        result = await self.lifecycle.execute("t/14", "reopen")
        assert result["status"] == "stopped"
        assert result["reason"] == "awaiting_feedback"

    async def test_cancelled_retry(self):
        await self._make_task("t/15", status="cancelled")
        result = await self.lifecycle.execute("t/15", "retry")
        assert result["status"] == "working"

    async def test_cancelled_resume(self):
        await self._make_task("t/16", status="cancelled", session_id="ses-456")
        result = await self.lifecycle.execute("t/16", "resume")
        assert result["status"] == "working"

    # --- System actions ---

    async def test_working_complete(self):
        from unittest.mock import AsyncMock, patch
        await self._make_task("t/17", status="working", worktree_path="/tmp/test-wt")
        with patch("switchboard.dispatch.gates._dispatch_review", new_callable=AsyncMock):
            result = await self.lifecycle.execute("t/17", "complete")
        assert result["status"] == "validating"

    async def test_working_exhaust_turns_with_gates(self):
        await self._make_task("t/18", status="working")
        result = await self.lifecycle.execute(
            "t/18", "exhaust_turns",
            project={"test_command": "pytest"},
        )
        assert result["status"] == "stopped"
        assert result["reason"] == "turns_exhausted"

    async def test_working_exhaust_turns_without_gates(self):
        await self._make_task("t/19", status="working")
        result = await self.lifecycle.execute(
            "t/19", "exhaust_turns",
            project={},
        )
        assert result["status"] == "stopped"
        assert result["reason"] == "turns_exhausted"

    async def test_working_timeout(self):
        await self._make_task("t/20", status="working")
        result = await self.lifecycle.execute("t/20", "timeout")
        assert result["status"] == "stopped"
        assert result["reason"] == "wall_clock_timeout"

    async def test_working_rate_limit(self):
        await self._make_task("t/21", status="working")
        result = await self.lifecycle.execute("t/21", "rate_limit")
        assert result["status"] == "stopped"
        assert result["reason"] == "rate_limited"

    async def test_working_error(self):
        await self._make_task("t/22", status="working")
        result = await self.lifecycle.execute("t/22", "error")
        assert result["status"] == "stopped"
        assert result["reason"] == "dispatch_error"

    async def test_validating_gate_pass(self):
        await self._make_task("t/23", status="validating")
        result = await self.lifecycle.execute("t/23", "gate_pass")
        assert result["status"] == "completed"
        assert result["reason"] == "gate_passed"

    async def test_validating_gate_fail(self):
        await self._make_task("t/24", status="validating")
        result = await self.lifecycle.execute(
            "t/24", "gate_fail", reason="max_test_retries",
        )
        assert result["status"] == "stopped"
        assert result["reason"] == "max_test_retries"

    async def test_validating_gate_fail_default_reason(self):
        await self._make_task("t/24b", status="validating")
        result = await self.lifecycle.execute("t/24b", "gate_fail")
        assert result["status"] == "stopped"
        assert result["reason"] == "gate_failed"

    async def test_validating_gate_retry(self):
        await self._make_task("t/25", status="validating")
        result = await self.lifecycle.execute("t/25", "gate_retry")
        assert result["status"] == "working"

    # --- Recovery ---

    async def test_working_recover_park(self):
        await self._make_task("t/26", status="working")
        result = await self.lifecycle.execute("t/26", "recover_park")
        assert result["status"] == "stopped"
        assert result["reason"] == "recovery_pending"

    async def test_stopped_recover_park(self):
        await self._make_task("t/27", status="stopped")
        result = await self.lifecycle.execute("t/27", "recover_park")
        assert result["status"] == "stopped"
        assert result["reason"] == "recovery_pending"

    async def test_stopped_recover_queue(self):
        await self._make_task("t/28", status="stopped")
        result = await self.lifecycle.execute("t/28", "recover_queue")
        assert result["status"] == "ready"

    async def test_stopped_recover_fail(self):
        await self._make_task("t/29", status="stopped")
        result = await self.lifecycle.execute("t/29", "recover_fail")
        assert result["status"] == "stopped"
        assert result["reason"] == "recovery_failed"

    async def test_working_recover_cancel(self):
        await self._make_task("t/30", status="working")
        result = await self.lifecycle.execute("t/30", "recover_cancel")
        assert result["status"] == "cancelled"

    async def test_working_recover_fail(self):
        await self._make_task("t/31", status="working")
        result = await self.lifecycle.execute("t/31", "recover_fail")
        assert result["status"] == "stopped"
        assert result["reason"] == "recovery_failed"

    # --- Audit log ---

    async def test_audit_log_written(self):
        await self._make_task("t/audit")
        await self.lifecycle.execute("t/audit", "dispatch")
        log = await self.db.get_audit_log("t/audit")
        # First entry is from create_task, second from execute
        lifecycle_entries = [e for e in log if e["action"] == "dispatch"]
        assert len(lifecycle_entries) == 1
        entry = lifecycle_entries[0]
        assert entry["previous_status"] == "ready"
        assert entry["new_status"] == "working"
        assert entry["triggered_by"] == "lifecycle"

    # --- Reason clearing ---

    async def test_reason_cleared_on_state_change(self):
        """When transitioning to a new state without explicit reason, reason is cleared."""
        await self._make_task("t/clear", status="stopped", reason="paused_by_user", session_id="ses-789")
        result = await self.lifecycle.execute("t/clear", "resume")
        assert result["status"] == "working"
        assert result["reason"] is None


# ---------------------------------------------------------------------------
# Attempt finalization
# ---------------------------------------------------------------------------

class TestAttemptFinalization:
    """Transitions out of working/validating must finalize the current attempt."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def _make_working_task(self, task_id, attempt=1):
        task = await self.db.create_task(id=task_id, project_id=PROJECT_ID, goal="test")
        await self.db.update_task(task_id, status="working", current_attempt=attempt)
        await self.db.create_attempt(task_id, attempt)
        return task

    async def _get_attempt(self, task_id, attempt=1):
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT * FROM task_attempts WHERE task_id = ? AND attempt_number = ?",
                (task_id, attempt),
            )
            return dict(rows[0]) if rows else None

    async def test_error_finalizes_attempt(self):
        await self._make_working_task("t/fin-1")
        await self.lifecycle.execute("t/fin-1", "error")
        attempt = await self._get_attempt("t/fin-1")
        assert attempt["finished_at"] is not None
        assert attempt["outcome"] == "dispatch_error"

    async def test_stop_finalizes_attempt(self):
        await self._make_working_task("t/fin-2")
        await self.lifecycle.execute("t/fin-2", "stop")
        attempt = await self._get_attempt("t/fin-2")
        assert attempt["finished_at"] is not None
        assert attempt["outcome"] == "paused_by_user"

    async def test_timeout_finalizes_attempt(self):
        await self._make_working_task("t/fin-3")
        await self.lifecycle.execute("t/fin-3", "timeout")
        attempt = await self._get_attempt("t/fin-3")
        assert attempt["finished_at"] is not None
        assert attempt["outcome"] == "wall_clock_timeout"

    async def test_gate_pass_finalizes_attempt(self):
        await self._make_working_task("t/fin-4")
        await self.db.update_task("t/fin-4", status="validating")
        await self.lifecycle.execute("t/fin-4", "gate_pass")
        attempt = await self._get_attempt("t/fin-4")
        assert attempt["finished_at"] is not None
        assert attempt["outcome"] == "gate_passed"

    async def test_gate_fail_finalizes_attempt(self):
        await self._make_working_task("t/fin-5")
        await self.db.update_task("t/fin-5", status="validating")
        await self.lifecycle.execute("t/fin-5", "gate_fail")
        attempt = await self._get_attempt("t/fin-5")
        assert attempt["finished_at"] is not None

    async def test_cancel_from_working_stores_cancelled(self):
        await self._make_working_task("t/fin-6")
        await self.lifecycle.execute("t/fin-6", "cancel")
        attempt = await self._get_attempt("t/fin-6")
        assert attempt["outcome"] == "cancelled", f"expected 'cancelled', got {attempt['outcome']!r}"

    async def test_resume_clears_attempt_finalization(self):
        await self._make_working_task("t/fin-7")
        # Set a session_id so resume precondition passes
        await self.db.update_task("t/fin-7", session_id="test-session-id")
        # Stop to finalize the attempt
        await self.lifecycle.execute("t/fin-7", "stop")
        attempt = await self._get_attempt("t/fin-7")
        assert attempt["finished_at"] is not None
        assert attempt["outcome"] == "paused_by_user"
        # Resume should clear finalization via _reopen_attempt
        await self.lifecycle.execute("t/fin-7", "resume")
        attempt = await self._get_attempt("t/fin-7")
        assert attempt["finished_at"] is None
        assert attempt["outcome"] is None

    async def test_gate_retry_clears_attempt_finalization(self):
        # Set up a task directly in validating with a finalized attempt
        # (mirrors the pattern in test_gate_pass_finalizes_attempt)
        await self._make_working_task("t/fin-8")
        await self.db.update_task("t/fin-8", status="validating")
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            await conn.execute(
                "UPDATE task_attempts SET finished_at = ?, outcome = ? WHERE task_id = ? AND attempt_number = ?",
                ("2026-01-01T00:00:00Z", "completed", "t/fin-8", 1),
            )
            await conn.commit()
        # gate_retry → working, _reopen_attempt clears finalization
        await self.lifecycle.execute("t/fin-8", "gate_retry")
        attempt = await self._get_attempt("t/fin-8")
        assert attempt["finished_at"] is None
        assert attempt["outcome"] is None

    async def test_complete_then_gate_pass_final_outcome(self):
        # Set up task in validating state with attempt outcome="completed"
        # (simulating what the working→complete transition writes via _finalize_attempt).
        # Then verify gate_pass overwrites with "gate_passed".
        await self._make_working_task("t/fin-9")
        await self.db.update_task("t/fin-9", status="validating")
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            await conn.execute(
                "UPDATE task_attempts SET finished_at = ?, outcome = ? WHERE task_id = ? AND attempt_number = ?",
                ("2026-01-01T00:00:00Z", "completed", "t/fin-9", 1),
            )
            await conn.commit()
        attempt = await self._get_attempt("t/fin-9")
        assert attempt["outcome"] == "completed"
        # gate_pass should overwrite with "gate_passed"
        await self.lifecycle.execute("t/fin-9", "gate_pass")
        attempt = await self._get_attempt("t/fin-9")
        assert attempt["outcome"] == "gate_passed"


# ---------------------------------------------------------------------------
# execute() — illegal transitions
# ---------------------------------------------------------------------------

class TestExecuteIllegalTransitions:
    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def _make_task(self, task_id, status="ready"):
        task = await self.db.create_task(
            id=task_id, project_id=PROJECT_ID, goal="test",
        )
        if status != "ready":
            task = await self.db.update_task(task_id, status=status)
        return task

    async def test_ready_resume_illegal(self):
        await self._make_task("t/bad1")
        with pytest.raises(IllegalTransition, match="Cannot 'resume' from state 'ready'"):
            await self.lifecycle.execute("t/bad1", "resume")

    async def test_ready_stop_illegal(self):
        await self._make_task("t/bad2")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute("t/bad2", "stop")

    async def test_working_dispatch_illegal(self):
        await self._make_task("t/bad3", status="working")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute("t/bad3", "dispatch")

    async def test_completed_dispatch_illegal(self):
        await self._make_task("t/bad4", status="completed")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute("t/bad4", "dispatch")

    async def test_cancelled_dispatch_illegal(self):
        await self._make_task("t/bad5", status="cancelled")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute("t/bad5", "dispatch")

    async def test_validating_dispatch_illegal(self):
        await self._make_task("t/bad6", status="validating")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute("t/bad6", "dispatch")

    async def test_illegal_transition_includes_available_actions(self):
        await self._make_task("t/bad7")
        with pytest.raises(IllegalTransition) as exc_info:
            await self.lifecycle.execute("t/bad7", "resume")
        err = exc_info.value
        assert "dispatch" in str(err)
        assert "cancel" in str(err)

    async def test_task_not_found(self):
        with pytest.raises(ValueError, match="not found"):
            await self.lifecycle.execute("nonexistent", "dispatch")

    async def test_nonsense_action(self):
        await self._make_task("t/bad8")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute("t/bad8", "fly_to_moon")


# ---------------------------------------------------------------------------
# execute() with preconditions and side effects
# ---------------------------------------------------------------------------

class TestPreconditionsAndSideEffects:
    @pytest.fixture(autouse=True)
    async def _setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def test_precondition_blocks_transition(self):
        task = await self.db.create_task(
            id="t/pre", project_id=PROJECT_ID, goal="test",
        )
        # Temporarily add a precondition that raises
        tdef = TRANSITIONS[("ready", "dispatch")]
        original_preconds = tdef.preconditions

        async def block(task, **ctx):
            raise ValueError("Blocked by precondition")

        tdef.preconditions = [block]
        try:
            with pytest.raises(ValueError, match="Blocked by precondition"):
                await self.lifecycle.execute("t/pre", "dispatch")
            # Task should NOT have changed
            task = await self.db.get_task("t/pre")
            assert task["status"] == "ready"
        finally:
            tdef.preconditions = original_preconds

    async def test_side_effect_runs_after_transition(self):
        task = await self.db.create_task(
            id="t/side", project_id=PROJECT_ID, goal="test",
        )
        tdef = TRANSITIONS[("ready", "dispatch")]
        original_effects = tdef.side_effects
        side_effect_called = []

        async def track(task, **ctx):
            side_effect_called.append(task["status"])

        tdef.side_effects = [track]
        try:
            await self.lifecycle.execute("t/side", "dispatch")
            assert side_effect_called == ["working"]
        finally:
            tdef.side_effects = original_effects

    async def test_side_effect_failure_does_not_rollback(self):
        task = await self.db.create_task(
            id="t/fail-side", project_id=PROJECT_ID, goal="test",
        )
        tdef = TRANSITIONS[("ready", "dispatch")]
        original_effects = tdef.side_effects

        async def boom(task, **ctx):
            raise RuntimeError("Side effect exploded")

        tdef.side_effects = [boom]
        try:
            # Should NOT raise — side effect errors are logged, not propagated
            result = await self.lifecycle.execute("t/fail-side", "dispatch")
            assert result["status"] == "working"
        finally:
            tdef.side_effects = original_effects


# ---------------------------------------------------------------------------
# get_available_actions tests
# ---------------------------------------------------------------------------

class TestGetAvailableActions:
    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def _make_task(self, task_id, status="ready"):
        task = await self.db.create_task(
            id=task_id, project_id=PROJECT_ID, goal="test",
        )
        if status != "ready":
            task = await self.db.update_task(task_id, status=status)
        return task

    async def test_ready_actions(self):
        await self._make_task("t/act1")
        actions = await self.lifecycle.get_available_actions("t/act1")
        names = {a["name"] for a in actions}
        assert "dispatch" in names
        assert "cancel" in names
        assert len(names) == 2

    async def test_working_actions(self):
        await self._make_task("t/act2", status="working")
        actions = await self.lifecycle.get_available_actions("t/act2")
        names = {a["name"] for a in actions}
        assert "stop" in names
        # Cancel not shown in dashboard for working state — Stop first, then Cancel from stopped
        assert "cancel" not in names

    async def test_validating_actions(self):
        await self._make_task("t/act2b", status="validating")
        actions = await self.lifecycle.get_available_actions("t/act2b")
        names = {a["name"] for a in actions}
        assert "stop" in names
        assert "skip_gate" in names
        # Cancel not shown in dashboard for validating state — Stop first, then Cancel from stopped
        assert "cancel" not in names

    async def test_stopped_actions(self):
        # stopped with no reason, no session — preconditions filter start/skip_gate/resume
        await self._make_task("t/act3", status="stopped")
        actions = await self.lifecycle.get_available_actions("t/act3")
        names = {a["name"] for a in actions}
        # resume: filtered (no session_id/worktree/gate-resumable state)
        # start: filtered (not awaiting_feedback)
        # skip_gate: filtered (not a gate failure reason)
        # recover: filtered (user_action=False)
        assert "retry" in names
        assert "end_task" in names  # compound action replaces cancel + close
        assert "cancel" not in names
        assert "close" not in names
        assert "resume" not in names
        assert "start" not in names
        assert "skip_gate" not in names
        assert "recover" not in names

    async def test_completed_actions(self):
        await self._make_task("t/act4", status="completed")
        actions = await self.lifecycle.get_available_actions("t/act4")
        names = {a["name"] for a in actions}
        assert "reopen" in names
        assert len(names) == 1

    async def test_cancelled_actions(self):
        # cancelled with no session_id — resume filtered by precondition
        await self._make_task("t/act5", status="cancelled")
        actions = await self.lifecycle.get_available_actions("t/act5")
        names = {a["name"] for a in actions}
        assert "retry" in names
        assert "resume" not in names  # filtered: no session_id

    async def test_action_includes_style_and_confirm(self):
        await self._make_task("t/act6")
        actions = await self.lifecycle.get_available_actions("t/act6")
        dispatch = next(a for a in actions if a["name"] == "dispatch")
        assert dispatch["style"] == "primary"
        assert dispatch["confirm"] is False
        cancel = next(a for a in actions if a["name"] == "cancel")
        assert cancel["style"] == "danger"
        assert cancel["confirm"] is True

    async def test_task_not_found_raises(self):
        with pytest.raises(ValueError, match="not found"):
            await self.lifecycle.get_available_actions("nonexistent")


# ---------------------------------------------------------------------------
# get_state_label tests
# ---------------------------------------------------------------------------

class TestGetStateLabel:
    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def _make_task(self, task_id, status="ready", reason=None):
        task = await self.db.create_task(
            id=task_id, project_id=PROJECT_ID, goal="test",
        )
        updates = {}
        if status != "ready":
            updates["status"] = status
        if reason is not None:
            updates["reason"] = reason
        if updates:
            task = await self.db.update_task(task_id, **updates)
        return task

    async def test_ready_no_reason(self):
        await self._make_task("t/lbl1")
        label = await self.lifecycle.get_state_label("t/lbl1")
        assert label["state"] == "ready"
        assert label["label"] == "Ready"
        assert label["color"] == "#6b7280"
        assert label["pulse"] is False

    async def test_working(self):
        await self._make_task("t/lbl2", status="working")
        label = await self.lifecycle.get_state_label("t/lbl2")
        assert label["label"] == "Working"
        assert label["pulse"] is True

    async def test_stopped_paused(self):
        await self._make_task("t/lbl3", status="stopped", reason="paused_by_user")
        label = await self.lifecycle.get_state_label("t/lbl3")
        assert label["label"] == "Paused"
        assert label["color"] == "#f59e0b"

    async def test_stopped_error(self):
        await self._make_task("t/lbl4", status="stopped", reason="dispatch_error")
        label = await self.lifecycle.get_state_label("t/lbl4")
        assert label["label"] == "Error"
        assert label["color"] == "#ef4444"

    async def test_completed_gate_passed(self):
        await self._make_task("t/lbl5", status="completed", reason="gate_passed")
        label = await self.lifecycle.get_state_label("t/lbl5")
        assert label["label"] == "Completed"
        assert label["color"] == "#10b981"

    async def test_completed_gate_skipped(self):
        await self._make_task("t/lbl6", status="completed", reason="gate_skipped")
        label = await self.lifecycle.get_state_label("t/lbl6")
        assert label["label"] == "Completed (Skipped)"

    async def test_completed_manually_closed(self):
        await self._make_task("t/lbl7", status="completed", reason="manually_closed")
        label = await self.lifecycle.get_state_label("t/lbl7")
        assert label["label"] == "Closed"

    async def test_cancelled(self):
        await self._make_task("t/lbl8", status="cancelled")
        label = await self.lifecycle.get_state_label("t/lbl8")
        assert label["label"] == "Cancelled"
        assert label["color"] == "#6b7280"

    async def test_stopped_unknown_reason_falls_back(self):
        await self._make_task("t/lbl9", status="stopped", reason="some_new_reason")
        label = await self.lifecycle.get_state_label("t/lbl9")
        # Falls back to (stopped, None) label
        assert label["state"] == "stopped"
        assert label["label"] == "Stopped"
        assert label["reason"] == "some_new_reason"

    async def test_validating_testing(self):
        await self._make_task("t/lbl10", status="validating", reason="testing")
        label = await self.lifecycle.get_state_label("t/lbl10")
        assert label["label"] == "Testing"
        assert label["pulse"] is True

    async def test_task_not_found_raises(self):
        with pytest.raises(ValueError, match="not found"):
            await self.lifecycle.get_state_label("nonexistent")

    async def test_all_state_labels_have_required_keys(self):
        """Every entry in STATE_LABELS has label, color, and pulse."""
        for key, info in STATE_LABELS.items():
            assert "label" in info, f"Missing 'label' for {key}"
            assert "color" in info, f"Missing 'color' for {key}"
            assert "pulse" in info, f"Missing 'pulse' for {key}"


# ---------------------------------------------------------------------------
# Transition table completeness
# ---------------------------------------------------------------------------

class TestTransitionTableCompleteness:
    def test_all_transitions_have_to_state(self):
        for key, tdef in TRANSITIONS.items():
            assert tdef.to_state is not None, f"Missing to_state for {key}"

    def test_transition_count(self):
        """Verify we have the expected number of transitions from the design."""
        # 16 user + 9 system + 7 recovery = 32, plus ("ready", "approve") = 37
        assert len(TRANSITIONS) == 37

    def test_all_user_actions_have_labels(self):
        """User-facing actions should have labels for dashboard buttons."""
        user_actions = [
            ("ready", "dispatch"), ("ready", "approve"), ("ready", "cancel"),
            ("working", "stop"),
            # ("working", "cancel") — intentionally no label; not shown in dashboard
            ("validating", "stop"), ("validating", "skip_gate"),
            # ("validating", "cancel") — intentionally no label; not shown in dashboard
            ("stopped", "resume"), ("stopped", "retry"), ("stopped", "start"),
            ("stopped", "skip_gate"), ("stopped", "cancel"), ("stopped", "close"),
            ("completed", "reopen"),
            ("cancelled", "retry"), ("cancelled", "resume"),
        ]
        for key in user_actions:
            assert TRANSITIONS[key].label, f"Missing label for user action {key}"

    def test_working_cancel_and_validating_cancel_have_no_label(self):
        """Cancel for working/validating has no label — not shown in dashboard UI.

        User flow: Stop first (lands in stopped), then Cancel from stopped.
        Transitions still exist for MCP tools and programmatic use.
        """
        assert not TRANSITIONS[("working", "cancel")].label
        assert not TRANSITIONS[("validating", "cancel")].label

    def test_status_map_covers_all_old_values(self):
        expected = {
            "pending-validation", "needs-review", "turns-exhausted",
            "rate-limited", "failed", "reopened", "merged", "blocked",
            "ready", "working", "validating", "stopped", "completed", "cancelled",
        }
        assert set(_STATUS_MAP.keys()) == expected

    def test_state_fallbacks_cover_all_states(self):
        expected = {"ready", "working", "validating", "stopped", "completed", "cancelled"}
        assert set(_STATE_FALLBACKS.keys()) == expected


# ---------------------------------------------------------------------------
# Service importability
# ---------------------------------------------------------------------------

class TestImportability:
    def test_lifecycle_module_imports(self):
        from switchboard.dispatch import lifecycle
        assert hasattr(lifecycle, "TaskLifecycle")
        assert hasattr(lifecycle, "TRANSITIONS")
        assert hasattr(lifecycle, "STATE_LABELS")
        assert hasattr(lifecycle, "IllegalTransition")
        assert hasattr(lifecycle, "TransitionDef")

    def test_lifecycle_class_instantiable(self):
        lc = TaskLifecycle()
        assert callable(lc.execute)
        assert callable(lc.get_available_actions)
        assert callable(lc.get_state_label)
        assert callable(lc._effective_state)

    def test_singleton_exists(self):
        from switchboard.dispatch.lifecycle import lifecycle
        assert isinstance(lifecycle, TaskLifecycle)


# ---------------------------------------------------------------------------
# Behavior tests — cancel / close / skip_gate through lifecycle.execute()
# ---------------------------------------------------------------------------

class TestCancelBehavior:
    """Test cancel transition through lifecycle with real DB."""

    @pytest.fixture(autouse=True)
    async def setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        try:
            await db.create_project(
                id=PROJECT_ID,
                repo="https://github.com/test/repo.git",
                working_dir="/tmp/lifecycle-test",
            )
        except Exception:
            pass

    async def test_cancel_from_working(self):
        await _seed(self.db, status="working")
        result = await self.lifecycle.execute(TASK_ID, "cancel")
        assert result["status"] == "cancelled"
        logs = await self.db.get_audit_log(TASK_ID)
        cancel_logs = [l for l in logs if l["action"] == "cancel"]
        assert len(cancel_logs) == 1

    async def test_cancel_from_ready(self):
        await _seed(self.db, status="ready")
        result = await self.lifecycle.execute(TASK_ID, "cancel")
        assert result["status"] == "cancelled"

    async def test_cancel_from_stopped(self):
        await _seed(self.db, status="stopped")
        result = await self.lifecycle.execute(TASK_ID, "cancel")
        assert result["status"] == "cancelled"

    async def test_cancel_from_validating(self):
        await _seed(self.db, status="pending-validation")
        result = await self.lifecycle.execute(TASK_ID, "cancel")
        assert result["status"] == "cancelled"

    async def test_cancel_from_completed_rejected(self):
        await _seed(self.db, status="completed")
        with pytest.raises(IllegalTransition, match="cancel"):
            await self.lifecycle.execute(TASK_ID, "cancel")

    async def test_cancel_from_cancelled_rejected(self):
        await _seed(self.db, status="cancelled")
        with pytest.raises(IllegalTransition, match="cancel"):
            await self.lifecycle.execute(TASK_ID, "cancel")

    async def test_cancel_sets_reason_cancelled(self):
        await _seed(self.db, status="stopped", reason="paused_by_user")
        result = await self.lifecycle.execute(TASK_ID, "cancel")
        assert result["status"] == "cancelled"
        assert result.get("reason") == "cancelled"


class TestCloseBehavior:
    """Test close transition through lifecycle with real DB."""

    @pytest.fixture(autouse=True)
    async def setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        try:
            await db.create_project(
                id=PROJECT_ID,
                repo="https://github.com/test/repo.git",
                working_dir="/tmp/lifecycle-test",
            )
        except Exception:
            pass

    async def test_close_from_stopped(self):
        from switchboard.dispatch.lifecycle import TRANSITIONS
        tdef = TRANSITIONS[("stopped", "close")]
        orig = tdef.side_effects[:]
        # Replace archive/cleanup side effect with no-op
        from unittest.mock import AsyncMock
        mock_cleanup = AsyncMock()
        tdef.side_effects = [mock_cleanup, tdef.side_effects[-1]]
        try:
            await _seed(self.db, status="stopped")
            result = await self.lifecycle.execute(TASK_ID, "close")
            assert result["status"] == "completed"
            assert result.get("reason") == "manually_closed"
        finally:
            tdef.side_effects = orig

    async def test_close_from_stopped_writes_audit(self):
        from switchboard.dispatch.lifecycle import TRANSITIONS
        from unittest.mock import AsyncMock
        tdef = TRANSITIONS[("stopped", "close")]
        orig = tdef.side_effects[:]
        tdef.side_effects = [AsyncMock(), tdef.side_effects[-1]]
        try:
            await _seed(self.db, status="stopped")
            await self.lifecycle.execute(TASK_ID, "close")
            logs = await self.db.get_audit_log(TASK_ID)
            close_logs = [l for l in logs if l["action"] == "close"]
            assert len(close_logs) == 1
        finally:
            tdef.side_effects = orig

    async def test_close_from_working_rejected(self):
        """close has a precondition that rejects working tasks."""
        await _seed(self.db, status="working")
        # working→close is not in the transition table, so IllegalTransition
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute(TASK_ID, "close")

    async def test_close_from_completed_rejected(self):
        await _seed(self.db, status="completed")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute(TASK_ID, "close")

    async def test_close_from_needs_review(self):
        """needs-review maps to stopped, so close should work."""
        from switchboard.dispatch.lifecycle import TRANSITIONS
        from unittest.mock import AsyncMock
        tdef = TRANSITIONS[("stopped", "close")]
        orig = tdef.side_effects[:]
        tdef.side_effects = [AsyncMock(), tdef.side_effects[-1]]
        try:
            await _seed(self.db, status="needs-review")
            result = await self.lifecycle.execute(TASK_ID, "close")
            assert result["status"] == "completed"
            assert result.get("reason") == "manually_closed"
        finally:
            tdef.side_effects = orig


class TestSkipGateBehavior:
    """Test skip_gate transition through lifecycle with real DB."""

    @pytest.fixture(autouse=True)
    async def setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        try:
            await db.create_project(
                id=PROJECT_ID,
                repo="https://github.com/test/repo.git",
                working_dir="/tmp/lifecycle-test",
            )
        except Exception:
            pass

    async def test_skip_gate_from_validating(self):
        from switchboard.dispatch.lifecycle import TRANSITIONS
        from unittest.mock import AsyncMock
        tdef = TRANSITIONS[("validating", "skip_gate")]
        orig = tdef.side_effects[:]
        # Mock dispatch_dependents only, keep gate field setting
        tdef.side_effects = [orig[0], orig[1], AsyncMock()]
        try:
            await _seed(self.db, status="pending-validation")
            result = await self.lifecycle.execute(TASK_ID, "skip_gate")
            assert result["status"] == "completed"
            assert result.get("reason") == "gate_skipped"
            # Verify gate_status was set by side effect
            task = await self.db.get_task(TASK_ID)
            assert task["gate_status"] == "passed"
            assert task["gate_passed_at"] is not None
        finally:
            tdef.side_effects = orig

    async def test_skip_gate_from_stopped(self):
        from switchboard.dispatch.lifecycle import TRANSITIONS
        from unittest.mock import AsyncMock
        tdef = TRANSITIONS[("stopped", "skip_gate")]
        orig = tdef.side_effects[:]
        tdef.side_effects = [orig[0], orig[1], AsyncMock()]
        try:
            # skip_gate from stopped requires a gate failure reason
            await _seed(self.db, status="stopped", reason="max_test_retries")
            result = await self.lifecycle.execute(TASK_ID, "skip_gate")
            assert result["status"] == "completed"
            assert result.get("reason") == "gate_skipped"
        finally:
            tdef.side_effects = orig

    async def test_skip_gate_from_completed_rejected(self):
        await _seed(self.db, status="completed")
        with pytest.raises(IllegalTransition, match="skip_gate"):
            await self.lifecycle.execute(TASK_ID, "skip_gate")

    async def test_skip_gate_from_working_rejected(self):
        await _seed(self.db, status="working")
        with pytest.raises(IllegalTransition, match="skip_gate"):
            await self.lifecycle.execute(TASK_ID, "skip_gate")

    async def test_skip_gate_writes_audit(self):
        from switchboard.dispatch.lifecycle import TRANSITIONS
        from unittest.mock import AsyncMock
        tdef = TRANSITIONS[("validating", "skip_gate")]
        orig = tdef.side_effects[:]
        # Keep all effects except dispatch_dependents (last one) — mock that out
        tdef.side_effects = orig[:-1] + [AsyncMock()]
        try:
            await _seed(self.db, status="pending-validation")
            await self.lifecycle.execute(TASK_ID, "skip_gate")
            logs = await self.db.get_audit_log(TASK_ID)
            skip_logs = [l for l in logs if l["action"] == "skip_gate"]
            assert len(skip_logs) == 1
            assert skip_logs[0]["new_status"] == "completed"
        finally:
            tdef.side_effects = orig

    async def test_skip_gate_posts_message(self):
        from switchboard.dispatch.lifecycle import TRANSITIONS
        from unittest.mock import AsyncMock
        tdef = TRANSITIONS[("validating", "skip_gate")]
        orig = tdef.side_effects[:]
        # Keep all effects except dispatch_dependents (last one) — mock that out
        tdef.side_effects = orig[:-1] + [AsyncMock()]
        try:
            await _seed(self.db, status="pending-validation")
            await self.lifecycle.execute(TASK_ID, "skip_gate")
            result = await self.db.read_task_messages(TASK_ID)
            messages = result["messages"]
            gate_msgs = [m for m in messages if m.get("title") == "Gate skipped"]
            assert len(gate_msgs) == 1
        finally:
            tdef.side_effects = orig


class TestCancelChainBehavior:
    """Test cancel_chain routes through lifecycle."""

    @pytest.fixture(autouse=True)
    async def setup(self, db, mock_git, mock_sdk):
        self.db = db
        try:
            await db.create_project(
                id="chain-proj",
                repo="https://github.com/test/repo.git",
                working_dir="/tmp/chain-test",
            )
        except Exception:
            pass
        await db.create_task(
            id="chain-proj/root", project_id="chain-proj", goal="root",
        )
        await db.update_task("chain-proj/root", status="working")
        await db.create_task(
            id="chain-proj/child", project_id="chain-proj", goal="child",
            depends_on="chain-proj/root",
        )
        await db.create_task(
            id="chain-proj/grandchild", project_id="chain-proj", goal="grandchild",
            depends_on="chain-proj/child",
        )

    async def test_cancel_chain_cancels_all(self):
        from switchboard.dispatch.engine import cancel_chain
        result = await cancel_chain("chain-proj/root")
        assert "chain-proj/root" in result["cancelled"]
        assert "chain-proj/child" in result["cancelled"]
        assert "chain-proj/grandchild" in result["cancelled"]
        for tid in ("chain-proj/root", "chain-proj/child", "chain-proj/grandchild"):
            task = await self.db.get_task(tid)
            assert task["status"] == "cancelled"

    async def test_cancel_chain_skips_completed(self):
        from switchboard.dispatch.engine import cancel_chain
        await self.db.update_task("chain-proj/child", status="completed")
        result = await cancel_chain("chain-proj/root")
        assert "chain-proj/child" not in result["cancelled"]
        # grandchild should still be cancelled (it's ready)
        assert "chain-proj/grandchild" in result["cancelled"]

    async def test_cancel_chain_writes_audit_via_lifecycle(self):
        from switchboard.dispatch.engine import cancel_chain
        await cancel_chain("chain-proj/root")
        logs = await self.db.get_audit_log("chain-proj/root")
        cancel_logs = [l for l in logs if l["action"] == "cancel"]
        assert len(cancel_logs) == 1
        assert cancel_logs[0]["triggered_by"] == "cancel-chain"


# ---------------------------------------------------------------------------
# Stop behavior tests
# ---------------------------------------------------------------------------


class TestStopBehavior:
    """Test stop_task transition through lifecycle with real DB."""

    @pytest.fixture(autouse=True)
    async def setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        try:
            await db.create_project(
                id=PROJECT_ID,
                repo="https://github.com/test/repo.git",
                working_dir="/tmp/lifecycle-test",
            )
        except Exception:
            pass

    async def test_stop_from_working(self):
        """Stop from working → stopped(paused_by_user), session_id preserved."""
        await _seed(self.db, status="working")
        # Set a session_id to verify preservation
        await self.db.update_task(TASK_ID, session_id="sess-123")
        result = await self.lifecycle.execute(TASK_ID, "stop")
        assert result["status"] == "stopped"
        assert result.get("reason") == "paused_by_user"
        # session_id must be preserved
        task = await self.db.get_task(TASK_ID)
        assert task["session_id"] == "sess-123"

    async def test_stop_from_validating_testing(self):
        """Stop from validating (testing) → stopped, gate_status preserved."""
        await _seed(self.db, status="pending-validation", gate_status="testing")
        result = await self.lifecycle.execute(TASK_ID, "stop")
        assert result["status"] == "stopped"
        assert result.get("reason") == "paused_by_user"
        # gate_status preserved (lifecycle only sets status+reason, not gate_status)
        task = await self.db.get_task(TASK_ID)
        assert task["gate_status"] == "testing"

    async def test_stop_from_validating_reviewing(self):
        """Stop from validating (reviewing) → stopped, gate_status preserved."""
        await _seed(self.db, status="pending-validation", gate_status="reviewing")
        result = await self.lifecycle.execute(TASK_ID, "stop")
        assert result["status"] == "stopped"
        assert result.get("reason") == "paused_by_user"
        task = await self.db.get_task(TASK_ID)
        assert task["gate_status"] == "reviewing"

    async def test_stop_from_ready_rejected(self):
        """Cannot stop a task that isn't running."""
        await _seed(self.db, status="ready")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute(TASK_ID, "stop")

    async def test_stop_from_stopped_rejected(self):
        """Cannot stop what's already stopped."""
        await _seed(self.db, status="stopped")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute(TASK_ID, "stop")

    async def test_stop_from_completed_rejected(self):
        """Cannot stop a completed task."""
        await _seed(self.db, status="completed")
        with pytest.raises(IllegalTransition):
            await self.lifecycle.execute(TASK_ID, "stop")

    async def test_stop_does_not_increment_attempt(self):
        """Stop is a pause, not a new run — current_attempt stays the same."""
        await _seed(self.db, status="working")
        await self.db.update_task(TASK_ID, current_attempt=2)
        await self.lifecycle.execute(TASK_ID, "stop")
        task = await self.db.get_task(TASK_ID)
        assert task["current_attempt"] == 2

    async def test_stop_posts_message(self):
        """Stop should post a status message."""
        await _seed(self.db, status="working")
        await self.lifecycle.execute(TASK_ID, "stop")
        result = await self.db.read_task_messages(TASK_ID)
        stop_msgs = [m for m in result["messages"] if m["title"] == "Task stopped"]
        assert len(stop_msgs) == 1
        assert "Session preserved" in stop_msgs[0]["content"]

    async def test_stop_drains_queue(self):
        """Stop from working should call _drain_queue (via side effect)."""
        from unittest.mock import AsyncMock, patch
        await _seed(self.db, status="working")
        with patch("switchboard.dispatch.queue._drain_queue", new_callable=AsyncMock) as mock_drain:
            await self.lifecycle.execute(TASK_ID, "stop")
            mock_drain.assert_called_once()

    async def test_stop_writes_audit_log(self):
        """Stop should write an audit log entry."""
        await _seed(self.db, status="working")
        await self.lifecycle.execute(TASK_ID, "stop")
        logs = await self.db.get_audit_log(TASK_ID)
        stop_logs = [l for l in logs if l["action"] == "stop"]
        assert len(stop_logs) == 1
        assert stop_logs[0]["previous_status"] == "working"
        assert stop_logs[0]["new_status"] == "stopped"

    async def test_resume_after_stop(self):
        """After stop, resume should work and preserve session_id."""
        await _seed(self.db, status="working")
        await self.db.update_task(TASK_ID, session_id="sess-456")
        await self.lifecycle.execute(TASK_ID, "stop")
        # Now resume
        result = await self.lifecycle.execute(TASK_ID, "resume")
        assert result["status"] == "working"
        task = await self.db.get_task(TASK_ID)
        assert task["session_id"] == "sess-456"

    async def test_cancel_after_stop(self):
        """After stop, cancel should work."""
        await _seed(self.db, status="working")
        await self.lifecycle.execute(TASK_ID, "stop")
        result = await self.lifecycle.execute(TASK_ID, "cancel")
        assert result["status"] == "cancelled"

    async def test_stop_preserves_gate_retries(self):
        """gate_retries must be preserved for gate resume."""
        await _seed(self.db, status="pending-validation", gate_status="testing")
        await self.db.update_task(TASK_ID, gate_retries=2)
        await self.lifecycle.execute(TASK_ID, "stop")
        task = await self.db.get_task(TASK_ID)
        assert task["gate_retries"] == 2

    async def test_stop_preserves_worktree_and_branch(self):
        """worktree_path and branch must be preserved for resume."""
        await _seed(self.db, status="working")
        await self.db.update_task(TASK_ID, worktree_path="/tmp/wt", branch="my-branch")
        await self.lifecycle.execute(TASK_ID, "stop")
        task = await self.db.get_task(TASK_ID)
        assert task["worktree_path"] == "/tmp/wt"
        assert task["branch"] == "my-branch"

    async def test_stop_validating_cancels_gate_task(self):
        """Stop from validating should cancel the gate asyncio task via _gate_tasks."""
        from switchboard.dispatch._state import _gate_tasks
        await _seed(self.db, status="pending-validation", gate_status="testing")
        # Create a mock gate asyncio task
        mock_task = asyncio.Future()
        _gate_tasks[TASK_ID] = mock_task
        try:
            await self.lifecycle.execute(TASK_ID, "stop")
            assert mock_task.cancelled()
            assert TASK_ID not in _gate_tasks
        finally:
            _gate_tasks.pop(TASK_ID, None)

    async def test_stop_validating_drains_queue(self):
        """Stop from validating should also drain the queue."""
        from unittest.mock import AsyncMock, patch
        await _seed(self.db, status="pending-validation", gate_status="testing")
        with patch("switchboard.dispatch.queue._drain_queue", new_callable=AsyncMock) as mock_drain:
            await self.lifecycle.execute(TASK_ID, "stop")
            mock_drain.assert_called_once()


# ---------------------------------------------------------------------------
# Reason-aware action filtering tests
# ---------------------------------------------------------------------------

class TestActionsFiltered:
    """Test get_available_actions for each (state, reason) pair in the button matrix."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id="filt-proj",
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def _make(self, task_id, status="ready", reason=None, **kwargs):
        task = await self.db.create_task(id=task_id, project_id="filt-proj", goal="test")
        updates = {}
        if status != "ready":
            updates["status"] = status
        if reason is not None:
            updates["reason"] = reason
        if kwargs:
            updates.update(kwargs)
        if updates:
            task = await self.db.update_task(task_id, **updates)
        return task

    async def _names(self, task_id):
        actions = await self.lifecycle.get_available_actions(task_id)
        return {a["name"] for a in actions}

    # ready sub-states
    async def test_ready_dispatchable(self):
        await self._make("f/r1")
        names = await self._names("f/r1")
        assert names == {"dispatch", "cancel"}

    async def test_ready_held(self):
        await self._make("f/r2", held=True)
        names = await self._names("f/r2")
        assert names == {"approve", "cancel"}

    async def test_ready_queued(self):
        from switchboard.db._helpers import now_iso
        await self._make("f/r3", queued_at=now_iso())
        names = await self._names("f/r3")
        assert names == {"cancel"}

    async def test_ready_blocked(self):
        await self._make("f/r4", status="blocked")
        names = await self._names("f/r4")
        assert names == {"cancel"}

    # working
    async def test_working(self):
        await self._make("f/w1", status="working")
        names = await self._names("f/w1")
        assert names == {"stop"}

    # validating
    async def test_validating(self):
        await self._make("f/v1", status="validating")
        names = await self._names("f/v1")
        assert names == {"stop", "skip_gate"}

    # stopped — paused_by_user / turns_exhausted / wall_clock_timeout / rate_limited
    async def test_stopped_paused_by_user(self):
        await self._make("f/s1", status="stopped", reason="paused_by_user",
                         session_id="sess123")
        names = await self._names("f/s1")
        assert "resume" in names
        assert "retry" in names
        assert "end_task" in names
        assert "close" not in names
        assert "cancel" not in names
        assert "skip_gate" not in names
        assert "start" not in names

    async def test_stopped_turns_exhausted_with_session(self):
        await self._make("f/s2", status="stopped", reason="turns_exhausted",
                         session_id="sess456")
        names = await self._names("f/s2")
        assert "resume" in names
        assert "retry" in names
        assert "end_task" in names
        assert "close" not in names
        assert "cancel" not in names
        assert "skip_gate" not in names

    async def test_stopped_rate_limited_with_session(self):
        await self._make("f/s3", status="stopped", reason="rate_limited",
                         session_id="sess789")
        names = await self._names("f/s3")
        assert "resume" in names
        assert "retry" in names
        assert "end_task" in names
        assert "close" not in names
        assert "cancel" not in names

    # stopped — gate failure reasons → skip_gate appears
    async def test_stopped_max_test_retries(self):
        await self._make("f/s4", status="stopped", reason="max_test_retries")
        names = await self._names("f/s4")
        assert "retry" in names
        assert "skip_gate" in names
        assert "end_task" in names
        assert "close" not in names
        assert "cancel" not in names
        assert "resume" not in names

    async def test_stopped_max_review_retries(self):
        await self._make("f/s5", status="stopped", reason="max_review_retries")
        names = await self._names("f/s5")
        assert "skip_gate" in names
        assert "retry" in names
        assert "end_task" in names
        assert "close" not in names

    async def test_stopped_review_stalled(self):
        await self._make("f/s6", status="stopped", reason="review_stalled")
        names = await self._names("f/s6")
        assert "skip_gate" in names
        assert "retry" in names
        assert "end_task" in names
        assert "close" not in names

    # stopped — dispatch_error / push_failed / worktree_missing → no skip_gate
    async def test_stopped_dispatch_error(self):
        await self._make("f/s7", status="stopped", reason="dispatch_error")
        names = await self._names("f/s7")
        assert "retry" in names
        assert "end_task" in names
        assert "close" not in names
        assert "cancel" not in names
        assert "skip_gate" not in names

    async def test_stopped_push_failed(self):
        await self._make("f/s8", status="stopped", reason="push_failed")
        names = await self._names("f/s8")
        assert "retry" in names
        assert "end_task" in names
        assert "close" not in names
        assert "cancel" not in names
        assert "skip_gate" not in names

    async def test_stopped_worktree_missing(self):
        await self._make("f/s11", status="stopped", reason="worktree_missing")
        names = await self._names("f/s11")
        assert "retry" in names
        assert "end_task" in names
        assert "close" not in names
        assert "cancel" not in names
        assert "skip_gate" not in names
        assert "resume" not in names

    # stopped — awaiting_feedback → start, cancel_reopen (no close)
    async def test_stopped_awaiting_feedback(self):
        await self._make("f/s9", status="stopped", reason="awaiting_feedback")
        names = await self._names("f/s9")
        assert "start" in names
        assert "cancel_reopen" in names
        assert "close" not in names
        assert "resume" not in names

    # stopped — recovery_limit → retry, end_task (no skip_gate)
    async def test_stopped_recovery_limit(self):
        await self._make("f/s10", status="stopped", reason="recovery_limit")
        names = await self._names("f/s10")
        assert "retry" in names
        assert "end_task" in names
        assert "close" not in names
        assert "cancel" not in names
        assert "skip_gate" not in names

    # completed
    async def test_completed(self):
        await self._make("f/c1", status="completed")
        names = await self._names("f/c1")
        assert names == {"reopen"}

    # cancelled
    async def test_cancelled_no_session(self):
        await self._make("f/ca1", status="cancelled")
        names = await self._names("f/ca1")
        assert names == {"retry"}

    async def test_cancelled_with_session(self):
        await self._make("f/ca2", status="cancelled", session_id="sess_can")
        names = await self._names("f/ca2")
        assert "retry" in names
        assert "resume" in names

    # Precondition filtering: resume without session filtered
    async def test_no_resume_without_session_stopped(self):
        await self._make("f/pr1", status="stopped", reason="paused_by_user")
        names = await self._names("f/pr1")
        assert "resume" not in names

    # Confirm flags — resume/retry on stopped must have confirm=False (no-confirm path)
    async def test_stopped_resume_retry_confirm_flags(self):
        """Resume and Retry on stopped tasks have confirm=False so they execute immediately."""
        await self._make("f/cf1", status="stopped", reason="paused_by_user",
                         session_id="sess_cf")
        actions = await self.lifecycle.get_available_actions("f/cf1")
        by_name = {a["name"]: a for a in actions}
        assert by_name["resume"]["confirm"] is False
        assert by_name["retry"]["confirm"] is False
        # end_task compound action has confirm=None (uses its own dialog)
        assert by_name["end_task"]["confirm"] is None

    # Compound end_task structure
    async def test_stopped_end_task_compound_structure(self):
        """end_task compound action has correct structure with options."""
        await self._make("f/et1", status="stopped", reason="paused_by_user")
        actions = await self.lifecycle.get_available_actions("f/et1")
        end_task = next(a for a in actions if a["name"] == "end_task")
        assert end_task["label"] == "End Task"
        assert end_task["style"] == "compound"
        assert end_task["confirm"] is None
        assert len(end_task["options"]) == 2
        close_opt = next(o for o in end_task["options"] if o["action"] == "close")
        cancel_opt = next(o for o in end_task["options"] if o["action"] == "cancel")
        assert close_opt["label"] == "Complete"
        assert close_opt["description"] == "Mark as done. Work and branch preserved."
        assert cancel_opt["label"] == "Discard"
        assert cancel_opt["description"] == "Mark as unwanted. Removed from active view."

    # System actions never appear
    async def test_no_system_actions_in_working(self):
        await self._make("f/sys1", status="working")
        names = await self._names("f/sys1")
        assert "complete" not in names
        assert "exhaust_turns" not in names
        assert "timeout" not in names
        assert "rate_limit" not in names
        assert "error" not in names
        assert "recover" not in names


# ---------------------------------------------------------------------------
# get_state_label: ready sub-state reason derivation
# ---------------------------------------------------------------------------

class TestGetStateLabelReadySubstates:
    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id="lbl-proj2",
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def test_ready_held_label(self):
        task = await self.db.create_task(id="lbl-proj2/h1", project_id="lbl-proj2", goal="test")
        await self.db.update_task("lbl-proj2/h1", held=True)
        label = await self.lifecycle.get_state_label("lbl-proj2/h1")
        assert label["label"] == "Held"

    async def test_ready_blocked_label(self):
        task = await self.db.create_task(id="lbl-proj2/b1", project_id="lbl-proj2", goal="test")
        await self.db.update_task("lbl-proj2/b1", status="blocked")
        label = await self.lifecycle.get_state_label("lbl-proj2/b1")
        assert label["label"] == "Blocked"

    async def test_ready_dispatchable_label(self):
        await self.db.create_task(id="lbl-proj2/d1", project_id="lbl-proj2", goal="test")
        label = await self.lifecycle.get_state_label("lbl-proj2/d1")
        assert label["label"] == "Ready"


# ---------------------------------------------------------------------------
# Dashboard API: GET /dashboard/api/tasks/{id}/actions endpoint
# ---------------------------------------------------------------------------

import json as _json


def _make_api_scope(path: str, method: str = "GET") -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "session_user": {"id": 1, "email": "owner@localhost", "name": "Owner", "role": "owner"},
    }


def _make_api_receive():
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return receive


class _ApiCapture:
    def __init__(self):
        self.status = None
        self.body = b""

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")

    def json(self):
        return _json.loads(self.body)


class TestDashboardActionsEndpoint:
    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        await db.create_project(
            id="api-act-proj",
            repo="https://github.com/test/repo.git",
            working_dir="/tmp",
        )
        self.task = await db.create_task(
            id="api-act-proj/t1",
            project_id="api-act-proj",
            goal="test actions endpoint",
        )

    async def test_actions_200_shape(self):
        from switchboard.dashboard.api import handle_request
        scope = _make_api_scope("/dashboard/api/tasks/api-act-proj%2Ft1/actions")
        resp = _ApiCapture()
        await handle_request(scope, _make_api_receive(), resp)
        assert resp.status == 200
        data = resp.json()
        assert data["task_id"] == "api-act-proj/t1"
        assert "state" in data
        assert "actions" in data
        state = data["state"]
        assert "status" in state
        assert "label" in state
        assert "color" in state
        assert "pulse" in state
        # ready task should have dispatch and cancel actions (hyphenated names)
        action_names = {a["name"] for a in data["actions"]}
        assert "dispatch" in action_names
        assert "cancel" in action_names
        # Each action has required fields
        for action in data["actions"]:
            assert "name" in action
            assert "label" in action
            assert "style" in action
            assert "confirm" in action

    async def test_actions_404_nonexistent(self):
        from switchboard.dashboard.api import handle_request
        scope = _make_api_scope("/dashboard/api/tasks/nonexistent%2Ftask/actions")
        resp = _ApiCapture()
        await handle_request(scope, _make_api_receive(), resp)
        assert resp.status == 404

    async def test_actions_hyphenated_names(self):
        """Action names returned by endpoint use hyphens, not underscores."""
        from switchboard.dashboard.api import handle_request
        # Set task to stopped with awaiting_feedback to get cancel_reopen action
        await self.db.update_task("api-act-proj/t1", status="stopped", reason="awaiting_feedback")
        scope = _make_api_scope("/dashboard/api/tasks/api-act-proj%2Ft1/actions")
        resp = _ApiCapture()
        await handle_request(scope, _make_api_receive(), resp)
        assert resp.status == 200
        data = resp.json()
        names = {a["name"] for a in data["actions"]}
        assert "cancel-reopen" in names  # cancel_reopen → cancel-reopen
        assert "cancel_reopen" not in names  # underscore version should NOT appear

    async def test_actions_state_matches_task(self):
        """State label in response matches lifecycle.get_state_label output."""
        from switchboard.dashboard.api import handle_request
        from switchboard.dispatch.lifecycle import lifecycle
        await self.db.update_task("api-act-proj/t1", status="working")
        scope = _make_api_scope("/dashboard/api/tasks/api-act-proj%2Ft1/actions")
        resp = _ApiCapture()
        await handle_request(scope, _make_api_receive(), resp)
        assert resp.status == 200
        data = resp.json()
        direct = await lifecycle.get_state_label("api-act-proj/t1")
        assert data["state"]["label"] == direct["label"]
        assert data["state"]["color"] == direct["color"]
