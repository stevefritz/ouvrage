"""Tests for v5 real-time output: review subtask, structured test output, attempt tracking."""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Review Subtask in get_task_status
# ---------------------------------------------------------------------------

class TestReviewSubtaskStatus:
    """get_task_status surfaces review subtask from subtasks table."""

    async def test_no_review_subtask_returns_none(self, db, sample_project):
        """Returns review_subtask=None when no review has run."""
        task = await db.create_task(
            id="test-project/task-no-review",
            project_id="test-project",
            goal="No review",
        )
        status = await db.get_task_status(task["id"])
        assert status["review_subtask"] is None

    async def test_review_subtask_working(self, db, sample_project):
        """Returns review_subtask with status=working when review is running."""
        task = await db.create_task(
            id="test-project/task-reviewing",
            project_id="test-project",
            goal="Under review",
        )
        # Create a subtask record (simulates _run_subtask creating it before running)
        await db.create_subtask(
            id="test-project/task-reviewing/review-1",
            task_id="test-project/task-reviewing",
            type="review",
            prompt="Review this code",
            model="opus",
        )

        status = await db.get_task_status("test-project/task-reviewing")
        review = status["review_subtask"]
        assert review is not None
        assert review["task_id"] == "test-project/task-reviewing/review-1"
        assert review["status"] == "working"
        assert review["model"] == "opus"
        assert review["session_id"] is None
        assert "elapsed" in review
        assert review["elapsed"].endswith("s")

    async def test_review_subtask_completed(self, db, sample_project):
        """Returns review_subtask with completed status and elapsed time."""
        task = await db.create_task(
            id="test-project/task-reviewed",
            project_id="test-project",
            goal="Review complete",
        )
        await db.create_subtask(
            id="test-project/task-reviewed/review-1",
            task_id="test-project/task-reviewed",
            type="review",
            prompt="Review this code",
            model="opus",
        )
        await db.update_subtask(
            "test-project/task-reviewed/review-1",
            status="completed",
            result="APPROVED",
            completed_at=db.now_iso(),
        )

        status = await db.get_task_status("test-project/task-reviewed")
        review = status["review_subtask"]
        assert review is not None
        assert review["status"] == "completed"
        assert "elapsed" in review

    async def test_review_subtask_returns_most_recent(self, db, sample_project):
        """Returns the most recent review subtask when multiple exist."""
        task = await db.create_task(
            id="test-project/task-multi-review",
            project_id="test-project",
            goal="Multiple reviews",
        )
        await db.create_subtask(
            id="test-project/task-multi-review/review-1",
            task_id="test-project/task-multi-review",
            type="review",
            prompt="Review this code",
            model="opus",
        )
        await db.update_subtask(
            "test-project/task-multi-review/review-1",
            status="completed",
            result="CHANGES REQUESTED",
            completed_at=db.now_iso(),
        )
        # Second review
        await db.create_subtask(
            id="test-project/task-multi-review/review-2",
            task_id="test-project/task-multi-review",
            type="review",
            prompt="Review this code again",
            model="opus",
        )

        status = await db.get_task_status("test-project/task-multi-review")
        review = status["review_subtask"]
        assert review["task_id"] == "test-project/task-multi-review/review-2"


# ---------------------------------------------------------------------------
# Structured Test Output
# ---------------------------------------------------------------------------

class TestStructuredTestOutput:
    """Test output is stored as structured JSON on the task."""

    @pytest.fixture(autouse=True)
    def _patches(self):
        patches = [
            patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake-worktree")),
            patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()),
            patch("switchboard.notifications.slack", AsyncMock()),
            patch("switchboard.dispatch.engine._ensure_branch_pushed", AsyncMock()),
            patch("switchboard.dispatch.engine._check_and_dispatch_dependents", AsyncMock()),
            patch("switchboard.dispatch.gates._dispatch_review", AsyncMock()),
            patch("switchboard.notifications.slack.task_needs_review", AsyncMock()),
            patch("switchboard.dispatch.engine.retry_task", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_test_output_stored_on_pass(self, db, sample_project):
        """On test pass, last_test_output is stored as structured JSON."""
        from switchboard.dispatch.gates import _run_test_gate

        task = await db.create_task(
            id="test-project/test-output-pass",
            project_id="test-project",
            goal="Test output",
            auto_review=False,
        )
        await db.update_task(task["id"], worktree_path="/tmp/fake-worktree", status="validating")

        with patch("switchboard.dispatch.gates._run_test_streaming", AsyncMock(return_value=("All tests passed\nOK", 0))):
            project = await db.get_project("test-project")
            task_fresh = await db.get_task(task["id"])
            await _run_test_gate(task["id"], project, task_fresh)

        updated = await db.get_task(task["id"])
        assert updated["last_test_output"] is not None
        output = json.loads(updated["last_test_output"])
        assert output["exit_code"] == 0
        assert "All tests passed" in output["stdout_tail"]
        assert "ran_at" in output
        assert output["attempt"] == 1

    async def test_test_output_stored_on_fail(self, db, sample_project):
        """On test fail, last_test_output stores exit_code=1 with stdout_tail."""
        from switchboard.dispatch.gates import _run_test_gate

        task = await db.create_task(
            id="test-project/test-output-fail",
            project_id="test-project",
            goal="Test output fail",
            auto_review=False,
        )
        await db.update_task(task["id"], worktree_path="/tmp/fake-worktree", status="completed",
                             max_gate_retries=0)

        with patch("switchboard.dispatch.gates._run_test_streaming", AsyncMock(return_value=("FAILED: 3 errors", 1))):
            project = await db.get_project("test-project")
            task_fresh = await db.get_task(task["id"])
            await _run_test_gate(task["id"], project, task_fresh)

        updated = await db.get_task(task["id"])
        assert updated["last_test_output"] is not None
        output = json.loads(updated["last_test_output"])
        assert output["exit_code"] == 1
        assert "FAILED" in output["stdout_tail"]
        assert output["attempt"] == 1

    async def test_test_output_capped_at_100_lines(self, db, sample_project):
        """stdout_tail is capped at the last 100 lines."""
        from switchboard.dispatch.gates import _run_test_gate

        task = await db.create_task(
            id="test-project/test-output-lines",
            project_id="test-project",
            goal="Test output lines",
            auto_review=False,
        )
        await db.update_task(task["id"], worktree_path="/tmp/fake-worktree", status="validating")

        # Generate 200 lines of output
        big_output = "\n".join(f"line {i}" for i in range(200))

        with patch("switchboard.dispatch.gates._run_test_streaming", AsyncMock(return_value=(big_output, 0))):
            project = await db.get_project("test-project")
            task_fresh = await db.get_task(task["id"])
            await _run_test_gate(task["id"], project, task_fresh)

        updated = await db.get_task(task["id"])
        output = json.loads(updated["last_test_output"])
        lines = output["stdout_tail"].split("\n")
        assert len(lines) <= 100
        # Should have the LAST 100 lines
        assert "line 199" in output["stdout_tail"]
        assert "line 0" not in output["stdout_tail"]

    async def test_last_test_output_parsed_in_get_task_status(self, db, sample_project):
        """get_task_status parses last_test_output from JSON string."""
        task = await db.create_task(
            id="test-project/test-status-output",
            project_id="test-project",
            goal="Status output",
        )
        output_data = {"exit_code": 0, "stdout_tail": "OK", "ran_at": "2026-01-01T00:00:00Z", "attempt": 1}
        await db.update_task(task["id"], last_test_output=json.dumps(output_data))

        status = await db.get_task_status(task["id"])
        # Should be parsed to dict, not raw string
        assert isinstance(status["last_test_output"], dict)
        assert status["last_test_output"]["exit_code"] == 0


# ---------------------------------------------------------------------------
# Attempt Tracking
# ---------------------------------------------------------------------------

class TestAttemptTracking:
    """Attempt number increments on retry, messages get stamped."""

    async def test_initial_attempt_is_1(self, db, sample_project):
        """New task starts with current_attempt=1."""
        task = await db.create_task(
            id="test-project/attempt-init",
            project_id="test-project",
            goal="Attempt init",
        )
        assert (task.get("current_attempt") or 1) == 1

    async def test_messages_stamped_with_attempt_1(self, db, sample_project):
        """Messages posted on a fresh task get attempt_number=1."""
        task = await db.create_task(
            id="test-project/attempt-stamp",
            project_id="test-project",
            goal="Attempt stamp",
        )
        msg = await db.post_task_message(
            task_id=task["id"],
            author="dispatcher",
            content="Initial message",
        )
        assert msg["attempt_number"] == 1

    async def test_retry_increments_attempt(self, db, sample_project):
        """retry_task increments current_attempt on the task."""
        from switchboard.dispatch.engine import retry_task

        task = await db.create_task(
            id="test-project/attempt-retry",
            project_id="test-project",
            goal="Attempt retry",
        )
        await db.update_task(task["id"], status="completed")

        with patch("switchboard.dispatch.engine.dispatch_task", AsyncMock(return_value={"status": "working"})):
            with patch("switchboard.dispatch.engine._invalidate_chain", AsyncMock()):
                await retry_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["current_attempt"] == 2

    async def test_retry_increments_again(self, db, sample_project):
        """Multiple retries keep incrementing current_attempt."""
        from switchboard.dispatch.engine import retry_task

        task = await db.create_task(
            id="test-project/attempt-multi-retry",
            project_id="test-project",
            goal="Multi retry",
        )
        await db.update_task(task["id"], status="completed")

        with patch("switchboard.dispatch.engine.dispatch_task", AsyncMock(return_value={"status": "working"})):
            with patch("switchboard.dispatch.engine._invalidate_chain", AsyncMock()):
                await retry_task(task["id"])
                await db.update_task(task["id"], status="completed")
                await retry_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["current_attempt"] == 3

    async def test_messages_stamped_with_current_attempt(self, db, sample_project):
        """After retry, new messages get attempt_number=2."""
        task = await db.create_task(
            id="test-project/attempt-stamp-2",
            project_id="test-project",
            goal="Attempt stamp 2",
        )
        # Post a message at attempt 1
        msg1 = await db.post_task_message(task_id=task["id"], author="cc-worker", content="Attempt 1 work")
        assert msg1["attempt_number"] == 1

        # Simulate retry — increment attempt
        await db.update_task(task["id"], current_attempt=2)

        # Post a message at attempt 2
        msg2 = await db.post_task_message(task_id=task["id"], author="cc-worker", content="Attempt 2 work")
        assert msg2["attempt_number"] == 2

    async def test_resume_does_not_increment_attempt(self, db, sample_project, mock_git, mock_sdk):
        """resume_task does NOT increment current_attempt (same attempt)."""
        from switchboard.dispatch.engine import resume_task

        task = await db.create_task(
            id="test-project/attempt-resume",
            project_id="test-project",
            goal="Resume no increment",
        )
        await db.update_task(task["id"], status="needs-review", current_attempt=2,
                             session_id="ses-resume-test")

        await resume_task(task["id"])

        # current_attempt should still be 2 (resume_task doesn't touch it)
        updated = await db.get_task(task["id"])
        assert updated["current_attempt"] == 2


# ---------------------------------------------------------------------------
# Dashboard API: /api/tasks/{id}/attempts
# ---------------------------------------------------------------------------

class TestAttemptsEndpoint:
    """get_task_attempts groups messages by attempt with outcome summaries."""

    async def test_empty_task_returns_empty_list(self, db, sample_project):
        """Task with no messages returns empty attempts list."""
        task = await db.create_task(
            id="test-project/attempts-empty",
            project_id="test-project",
            goal="Empty attempts",
        )
        attempts = await db.get_task_attempts(task["id"])
        assert attempts == []

    async def test_single_attempt_in_progress(self, db, sample_project):
        """Single attempt with only progress messages is in-progress."""
        task = await db.create_task(
            id="test-project/attempts-single",
            project_id="test-project",
            goal="Single attempt",
        )
        await db.post_task_message(task_id=task["id"], author="cc-worker", content="Working on it")

        attempts = await db.get_task_attempts(task["id"])
        assert len(attempts) == 1
        assert attempts[0]["attempt_number"] == 1
        assert attempts[0]["outcome"] == "in-progress"
        assert len(attempts[0]["messages"]) == 1

    async def test_test_failure_outcome(self, db, sample_project):
        """Attempt ending in test failure is marked test-failure."""
        task = await db.create_task(
            id="test-project/attempts-test-fail",
            project_id="test-project",
            goal="Test failure attempt",
        )
        await db.post_task_message(task_id=task["id"], author="cc-worker", content="Done")
        # Post test failure
        await db.post_task_message(
            task_id=task["id"], author="dispatcher", type="test-result",
            title="Tests failed (attempt 1/3)", content="```\nFAILED\n```",
        )
        # Increment to attempt 2
        await db.update_task(task["id"], current_attempt=2)
        await db.post_task_message(task_id=task["id"], author="cc-worker", content="Fixed")

        attempts = await db.get_task_attempts(task["id"])
        assert len(attempts) == 2
        assert attempts[0]["attempt_number"] == 1
        assert attempts[0]["outcome"] == "test-failure"
        assert attempts[1]["attempt_number"] == 2
        assert attempts[1]["outcome"] == "in-progress"

    async def test_review_rejection_outcome(self, db, sample_project):
        """Attempt ending in review rejection is marked review-rejection."""
        task = await db.create_task(
            id="test-project/attempts-review-reject",
            project_id="test-project",
            goal="Review rejection",
        )
        await db.post_task_message(task_id=task["id"], author="cc-worker", content="Done")
        await db.post_task_message(
            task_id=task["id"], author="cc-worker", type="review",
            title="CHANGES REQUESTED", content="Fix these issues",
        )
        # Increment to attempt 2
        await db.update_task(task["id"], current_attempt=2)
        await db.post_task_message(task_id=task["id"], author="cc-worker", content="Revised")

        attempts = await db.get_task_attempts(task["id"])
        assert attempts[0]["outcome"] == "review-rejection"

    async def test_success_outcome_on_approved_review(self, db, sample_project):
        """Last attempt with APPROVED review is marked success."""
        task = await db.create_task(
            id="test-project/attempts-approved",
            project_id="test-project",
            goal="Approved",
        )
        await db.post_task_message(task_id=task["id"], author="cc-worker", content="Done")
        await db.post_task_message(
            task_id=task["id"], author="cc-worker", type="review",
            title="APPROVED", content="Looks good!",
        )

        attempts = await db.get_task_attempts(task["id"])
        assert len(attempts) == 1
        assert attempts[0]["outcome"] == "success"

    async def test_success_outcome_on_task_completed_status(self, db, sample_project):
        """Dispatcher 'Task completed' status message marks attempt as success."""
        task = await db.create_task(
            id="test-project/attempts-completed",
            project_id="test-project",
            goal="Task completed without gate",
        )
        await db.post_task_message(task_id=task["id"], author="cc-worker", type="result", content="Here are the results")
        await db.post_task_message(
            task_id=task["id"], author="dispatcher", type="status",
            title="Task completed", content="CC session completed successfully.",
        )

        attempts = await db.get_task_attempts(task["id"])
        assert len(attempts) == 1
        assert attempts[0]["outcome"] == "success"

    async def test_invalid_task_raises_error(self, db, sample_project):
        """get_task_attempts raises ValueError for unknown task."""
        with pytest.raises(ValueError, match="not found"):
            await db.get_task_attempts("test-project/nonexistent")


# ---------------------------------------------------------------------------
# Dashboard API handler via dashboard_api module
# ---------------------------------------------------------------------------

class TestDashboardApiAttempts:
    """_handle_get_attempts returns attempts via REST."""

    async def test_attempts_endpoint(self, db, sample_project):
        """Dashboard API returns attempts for a task (from disk archives)."""
        from switchboard.dashboard.api import handle_request

        task = await db.create_task(
            id="test-project/api-attempts",
            project_id="test-project",
            goal="API attempts",
        )

        send_calls = []

        async def mock_send(event):
            send_calls.append(event)

        scope = {
            "path": f"/dashboard/api/tasks/test-project/api-attempts/attempts",
            "method": "GET",
            "query_string": b"",
        }
        await handle_request(scope, None, mock_send)

        # Find the response body
        body_event = next((e for e in send_calls if e.get("type") == "http.response.body"), None)
        assert body_event is not None
        data = json.loads(body_event["body"])
        assert "attempts" in data
        assert isinstance(data["attempts"], list)
        # No disk archives exist yet, so attempts list is empty
        assert data["task_id"] == "test-project/api-attempts"

    async def test_task_detail_includes_review_subtask_field(self, db, sample_project):
        """GET /api/tasks/{id} includes review_subtask in response."""
        from switchboard.dashboard.api import handle_request

        task = await db.create_task(
            id="test-project/api-review-sub",
            project_id="test-project",
            goal="Review sub field",
        )

        send_calls = []

        async def mock_send(event):
            send_calls.append(event)

        scope = {
            "path": "/dashboard/api/tasks/test-project/api-review-sub",
            "method": "GET",
            "query_string": b"",
        }
        await handle_request(scope, None, mock_send)

        body_event = next((e for e in send_calls if e.get("type") == "http.response.body"), None)
        data = json.loads(body_event["body"])
        assert "review_subtask" in data
        assert data["review_subtask"] is None  # no review yet

    async def test_task_detail_includes_last_test_output(self, db, sample_project):
        """GET /api/tasks/{id} includes last_test_output parsed as dict."""
        from switchboard.dashboard.api import handle_request

        task = await db.create_task(
            id="test-project/api-test-output",
            project_id="test-project",
            goal="Test output field",
        )
        output_data = {"exit_code": 0, "stdout_tail": "OK", "ran_at": "2026-01-01T00:00:00Z", "attempt": 1}
        await db.update_task(task["id"], last_test_output=json.dumps(output_data))

        send_calls = []

        async def mock_send(event):
            send_calls.append(event)

        scope = {
            "path": "/dashboard/api/tasks/test-project/api-test-output",
            "method": "GET",
            "query_string": b"",
        }
        await handle_request(scope, None, mock_send)

        body_event = next((e for e in send_calls if e.get("type") == "http.response.body"), None)
        data = json.loads(body_event["body"])
        assert "last_test_output" in data
        assert isinstance(data["last_test_output"], dict)
        assert data["last_test_output"]["exit_code"] == 0
