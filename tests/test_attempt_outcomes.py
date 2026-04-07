"""Tests for attempt outcome gaps fixes.

Covers: heuristic logic, OUTCOME_DEFINITIONS completeness, _finalize_attempt
context key, transition side effects, and in-progress shortcut.
"""
import pytest
from switchboard.db._helpers import _determine_attempt_outcome


# ---------------------------------------------------------------------------
# Heuristic: _determine_attempt_outcome
# ---------------------------------------------------------------------------


class TestDetermineAttemptOutcome:
    """Unit tests for the message-based outcome heuristic."""

    def _msg(self, author="dispatcher", msg_type="status", title=""):
        return {"author": author, "type": msg_type, "title": title, "content": ""}

    def test_tests_passed_not_last_returns_success(self):
        """GAP-2: Tests passed on a non-last attempt should be 'success', not 'test-failure'."""
        messages = [
            self._msg(msg_type="test-result", title="TESTS PASSED"),
        ]
        # is_last=False, has_next=True — reopen-after-success scenario
        result = _determine_attempt_outcome(messages, is_last=False, has_next=True)
        assert result == "success"

    def test_tests_passed_is_last_returns_success(self):
        messages = [
            self._msg(msg_type="test-result", title="TESTS PASSED"),
        ]
        result = _determine_attempt_outcome(messages, is_last=True, has_next=False)
        assert result == "success"

    def test_tests_passed_but_review_rejected(self):
        """GAP-2 nuance: Tests passed but review rejected — should return review-rejection."""
        messages = [
            self._msg(msg_type="test-result", title="TESTS PASSED"),
            self._msg(author="reviewer", msg_type="review", title="CHANGES REQUESTED"),
        ]
        result = _determine_attempt_outcome(messages, is_last=False, has_next=True)
        assert result == "review-rejection"

    def test_tests_failed_returns_test_failure(self):
        messages = [
            self._msg(msg_type="test-result", title="TESTS FAILED"),
        ]
        result = _determine_attempt_outcome(messages, is_last=False, has_next=True)
        assert result == "test-failure"

    def test_resume_after_error_discards_terminal_event(self):
        """Resume awareness: error followed by RESUMED should not return 'error'."""
        messages = [
            self._msg(msg_type="status", title="DISPATCH ERROR"),
            self._msg(msg_type="status", title="RESUMED"),
            self._msg(author="cc-worker", msg_type="progress", title="Working again"),
        ]
        result = _determine_attempt_outcome(messages, is_last=True, has_next=False)
        assert result == "in-progress"

    def test_resume_after_failed_discards_terminal_event(self):
        """Resume awareness: FAILED followed by RESUMED should not return 'error'."""
        messages = [
            self._msg(msg_type="status", title="TASK FAILED"),
            self._msg(msg_type="status", title="RESUMED"),
        ]
        result = _determine_attempt_outcome(messages, is_last=True, has_next=False)
        assert result == "in-progress"

    def test_error_without_resume_returns_error(self):
        """Error without subsequent resume should still return 'error'."""
        messages = [
            self._msg(msg_type="status", title="DISPATCH ERROR"),
        ]
        result = _determine_attempt_outcome(messages, is_last=True, has_next=False)
        assert result == "error"

    def test_wall_clock_timeout(self):
        messages = [self._msg(title="WALL CLOCK TIMEOUT")]
        assert _determine_attempt_outcome(messages, True, False) == "wall-clock-timeout"

    def test_turns_exhausted(self):
        messages = [self._msg(title="TURNS EXHAUSTED")]
        assert _determine_attempt_outcome(messages, True, False) == "turns-exhausted"

    def test_review_approved_last(self):
        messages = [self._msg(msg_type="review", title="APPROVED")]
        assert _determine_attempt_outcome(messages, True, False) == "success"

    def test_no_terminal_event_has_next(self):
        messages = [self._msg(author="cc-worker", msg_type="progress", title="WIP")]
        assert _determine_attempt_outcome(messages, False, True) == "retried"

    def test_no_terminal_event_is_last(self):
        messages = [self._msg(author="cc-worker", msg_type="progress", title="WIP")]
        assert _determine_attempt_outcome(messages, True, False) == "in-progress"


# ---------------------------------------------------------------------------
# OUTCOME_DEFINITIONS completeness
# ---------------------------------------------------------------------------


class TestOutcomeDefinitions:
    """Verify OUTCOME_DEFINITIONS has all required keys with correct labels/colors."""

    def test_required_keys_present(self):
        from switchboard.dispatch.lifecycle import OUTCOME_DEFINITIONS
        required = [
            "gate_passed", "gate_skipped", "completed", "success",
            "test-failure", "review-rejection", "error", "failed",
            "cancelled", "in-progress", "retried",
            "wall_clock_timeout", "turns_exhausted",
            "wall-clock-timeout", "turns-exhausted",
            "test_failure", "review_rejected",
            "awaiting_feedback", "manually_closed",
            "max_test_retries", "max_review_retries",
            "review_stalled", "gate_failed",
        ]
        for key in required:
            assert key in OUTCOME_DEFINITIONS, f"Missing OUTCOME_DEFINITIONS key: {key}"

    def test_all_labels_lowercase(self):
        from switchboard.dispatch.lifecycle import OUTCOME_DEFINITIONS
        for key, defn in OUTCOME_DEFINITIONS.items():
            label = defn["label"]
            assert label == label.lower(), f"Label for '{key}' is not lowercase: '{label}'"

    def test_success_colors_standardized(self):
        from switchboard.dispatch.lifecycle import OUTCOME_DEFINITIONS
        success_keys = ["gate_passed", "gate_skipped", "completed", "success"]
        for key in success_keys:
            assert OUTCOME_DEFINITIONS[key]["color"] == "#22c55e", \
                f"Color for '{key}' should be #22c55e, got {OUTCOME_DEFINITIONS[key]['color']}"


# ---------------------------------------------------------------------------
# _finalize_attempt with outcome context key
# ---------------------------------------------------------------------------


class TestFinalizeAttemptOutcomeKey:
    """Test that _finalize_attempt respects ctx.get('outcome')."""

    async def test_outcome_from_context(self, db, sample_project):
        """GAP-10: outcome passed via context should take priority."""
        from switchboard.dispatch.lifecycle import _finalize_attempt

        task = await db.create_task(id="test-project/finalize-1", project_id="test-project", goal="Test finalize")
        await db.update_task(task["id"], status="working", current_attempt=1)
        await db.create_attempt(task["id"], 1)

        task = await db.get_task(task["id"])
        await _finalize_attempt(task, outcome="test_failure", _previous_status="validating")

        rows = await db.get_attempt(task["id"], 1)
        assert rows["outcome"] == "test_failure"

    async def test_outcome_falls_back_to_reason(self, db, sample_project):
        """Without context outcome, should use task reason."""
        from switchboard.dispatch.lifecycle import _finalize_attempt

        task = await db.create_task(id="test-project/finalize-2", project_id="test-project", goal="Test finalize fallback")
        await db.update_task(task["id"], status="stopped", reason="paused_by_user", current_attempt=1)
        await db.create_attempt(task["id"], 1)

        task = await db.get_task(task["id"])
        await _finalize_attempt(task, _previous_status="working")

        rows = await db.get_attempt(task["id"], 1)
        assert rows["outcome"] == "paused_by_user"

    async def test_outcome_falls_back_to_previous_status(self, db, sample_project):
        """Without context outcome or reason, should use _previous_status."""
        from switchboard.dispatch.lifecycle import _finalize_attempt

        task = await db.create_task(id="test-project/finalize-3", project_id="test-project", goal="Test finalize fallback 2")
        await db.update_task(task["id"], status="stopped", current_attempt=1)
        await db.create_attempt(task["id"], 1)

        task = await db.get_task(task["id"])
        await _finalize_attempt(task, _previous_status="working")

        rows = await db.get_attempt(task["id"], 1)
        assert rows["outcome"] == "working"


# ---------------------------------------------------------------------------
# get_task_attempts in-progress shortcut
# ---------------------------------------------------------------------------


class TestGetTaskAttemptsInProgress:
    """Test that open attempts (finished_at IS NULL) return in-progress."""

    async def test_open_attempt_returns_in_progress(self, db, sample_project):
        """GAP: Open attempt record should bypass heuristic and return in-progress."""
        task = await db.create_task(id="test-project/in-progress-1", project_id="test-project", goal="In progress test")
        await db.update_task(task["id"], status="working", current_attempt=1)
        await db.create_attempt(task["id"], 1)

        # Post a message that would normally trigger "error" in the heuristic
        await db.post_task_message(
            task_id=task["id"], author="dispatcher", type="status",
            title="DISPATCH ERROR", content="Something broke",
        )

        attempts = await db.get_task_attempts(task["id"])
        assert len(attempts) == 1
        # Because attempt is open (finished_at=NULL), it should be in-progress
        assert attempts[0]["outcome"] == "in-progress"

    async def test_finished_attempt_uses_stored_outcome(self, db, sample_project):
        """Finished attempt with stored outcome should use that outcome."""
        task = await db.create_task(id="test-project/finished-1", project_id="test-project", goal="Finished test")
        await db.update_task(task["id"], status="stopped", current_attempt=1)
        await db.create_attempt(task["id"], 1)
        await db.update_attempt(task["id"], 1, finished_at="2026-01-01T00:00:00Z", outcome="test_failure")

        await db.post_task_message(
            task_id=task["id"], author="cc-worker", content="Done",
        )

        attempts = await db.get_task_attempts(task["id"])
        assert attempts[0]["outcome"] == "test_failure"


# ---------------------------------------------------------------------------
# Transition side effects: stopped → skip_gate / close / cancel
# ---------------------------------------------------------------------------


class TestTransitionSideEffects:
    """Verify transitions include _finalize_attempt where required."""

    def test_stopped_skip_gate_has_finalize(self):
        from switchboard.dispatch.lifecycle import TRANSITIONS, _finalize_attempt
        t = TRANSITIONS[("stopped", "skip_gate")]
        assert _finalize_attempt in t.side_effects

    def test_stopped_close_has_finalize(self):
        from switchboard.dispatch.lifecycle import TRANSITIONS, _finalize_attempt
        t = TRANSITIONS[("stopped", "close")]
        assert _finalize_attempt in t.side_effects

    def test_stopped_cancel_has_finalize_and_reason(self):
        from switchboard.dispatch.lifecycle import TRANSITIONS, _finalize_attempt
        t = TRANSITIONS[("stopped", "cancel")]
        assert _finalize_attempt in t.side_effects
        assert t.reason == "cancelled"

    def test_validating_retry_has_finalize_before_launch(self):
        from switchboard.dispatch.lifecycle import TRANSITIONS, _finalize_attempt, _retry_launch_session
        t = TRANSITIONS[("validating", "retry")]
        effects = t.side_effects
        assert _finalize_attempt in effects
        assert _retry_launch_session in effects
        # _finalize_attempt must come before _retry_launch_session
        assert effects.index(_finalize_attempt) < effects.index(_retry_launch_session)
