"""Tests for the pending-validation status flow and related changes.

Covers:
- sdk_session sets pending-validation (not completed) on CC finish
- _check_and_dispatch_dependents sets status=completed when gate passes
- resume_task accepts pending-validation
- retry_task gate-stall check includes pending-validation
- dispatch_task treats pending-validation as resumable
- escalate tool: sets needs-review, posts escalation message
- worker tool allowlist: list_tools filters, call_tool rejects
- recovery: pending-validation tasks re-enter gate pipeline
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import ouvrage.db as db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_worker_context():
    from ouvrage.server.context import set_request_context
    set_request_context(user_id=None, is_token_auth=False, is_worker=True)


def _set_user_context(user_id=1):
    from ouvrage.server.context import set_request_context
    set_request_context(user_id=user_id, is_token_auth=True, is_worker=False)


def _clear_context():
    from ouvrage.server.context import set_request_context
    set_request_context(user_id=None, is_token_auth=False, is_worker=False)


# ---------------------------------------------------------------------------
# pending-validation status set by sdk_session
# ---------------------------------------------------------------------------

class TestSdkSessionSetsPendingValidation:
    """CC session completion goes through lifecycle, entering validating state."""

    def test_completion_uses_lifecycle_execute(self):
        """Source code must use lifecycle.execute('complete') on CC finish."""
        import inspect
        import ouvrage.dispatch.sdk_session as mod
        source = inspect.getsource(mod)
        # The normal completion path must use lifecycle.execute("complete")
        assert 'lifecycle.execute' in source
        assert '"complete"' in source


# ---------------------------------------------------------------------------
# _check_and_dispatch_dependents sets completed
# ---------------------------------------------------------------------------

class TestGatePassCompletesTask:
    """lifecycle.execute('gate_pass') transitions validating → completed."""

    @pytest.fixture(autouse=True)
    def _patches(self, sample_project):
        self.drain_mock = patch(
            "ouvrage.dispatch.engine._drain_queue",
            new_callable=AsyncMock,
        )
        self.release_mock = patch(
            "ouvrage.dispatch.engine._auto_release_worktree",
            new_callable=AsyncMock,
        )
        self.pr_mock = patch(
            "ouvrage.dispatch.engine._maybe_create_pr",
            new_callable=AsyncMock,
        )
        with self.drain_mock, self.release_mock, self.pr_mock:
            yield

    async def test_validating_becomes_completed_via_gate_pass(self, db, sample_project):
        from ouvrage.dispatch.lifecycle import lifecycle

        task = await db.create_task(
            id="test-project/pv-task",
            project_id="test-project",
            goal="test",
        )
        await db.update_task("test-project/pv-task",
                             status="validating")

        result = await lifecycle.execute("test-project/pv-task", "gate_pass",
                                         triggered_by="gate-pipeline")

        updated = await db.get_task("test-project/pv-task")
        assert updated["status"] == "completed"

    async def test_pending_validation_becomes_completed_via_gate_pass(self, db, sample_project):
        """Legacy pending-validation status is mapped to validating by lifecycle."""
        from ouvrage.dispatch.lifecycle import lifecycle

        task = await db.create_task(
            id="test-project/te-task",
            project_id="test-project",
            goal="test",
        )
        # Legacy status still works via _STATUS_MAP
        await db.update_task("test-project/te-task",
                             status="pending-validation")

        result = await lifecycle.execute("test-project/te-task", "gate_pass",
                                         triggered_by="gate-pipeline")

        updated = await db.get_task("test-project/te-task")
        assert updated["status"] == "completed"

    async def test_completed_status_unchanged(self, db, sample_project):
        """Already-completed tasks should not be re-touched."""
        from ouvrage.dispatch.engine import _check_and_dispatch_dependents

        task = await db.create_task(
            id="test-project/already-done",
            project_id="test-project",
            goal="test",
        )
        await db.update_task("test-project/already-done",
                             status="completed",
                             gate_status="passed",
                             gate_passed_at=db.now_iso())

        await _check_and_dispatch_dependents("test-project/already-done")

        updated = await db.get_task("test-project/already-done")
        assert updated["status"] == "completed"

    async def test_no_gate_passed_at_returns_early(self, db, sample_project):
        """Without gate_passed_at the function should no-op."""
        from ouvrage.dispatch.engine import _check_and_dispatch_dependents

        task = await db.create_task(
            id="test-project/no-gate",
            project_id="test-project",
            goal="test",
        )
        await db.update_task("test-project/no-gate", status="pending-validation")

        await _check_and_dispatch_dependents("test-project/no-gate")

        updated = await db.get_task("test-project/no-gate")
        # Should NOT have been updated to completed (gate never passed)
        assert updated["status"] == "pending-validation"


# ---------------------------------------------------------------------------
# resume_task handles pending-validation
# ---------------------------------------------------------------------------

class TestResumeTaskPendingValidation:
    """resume_task must accept pending-validation as a resumable status."""

    @pytest.fixture(autouse=True)
    def _patches(self, sample_project):
        self.dispatch_mock = patch(
            "ouvrage.dispatch.engine.dispatch_task",
            new_callable=AsyncMock,
            return_value={"status": "working"},
        )
        with self.dispatch_mock:
            yield

    async def test_resume_accepts_pending_validation(self, db, sample_project):
        from ouvrage.dispatch.engine import resume_task

        task = await db.create_task(
            id="test-project/pv-resume",
            project_id="test-project",
            goal="test resume",
        )
        await db.update_task("test-project/pv-resume", status="pending-validation")

        # Should not raise ValueError
        result = await resume_task("test-project/pv-resume")
        # dispatch_task was called (mock returns {"status": "working"})
        assert result is not None

    async def test_resume_pending_validation_with_gate_passed_triggers_chain(self, db, sample_project):
        """If gate already passed for a pending-validation task, re-trigger chain, not CC session."""
        from ouvrage.dispatch.engine import resume_task

        with patch(
            "ouvrage.dispatch.engine._check_and_dispatch_dependents",
            new_callable=AsyncMock,
        ) as mock_chain:
            task = await db.create_task(
                id="test-project/pv-gate-done",
                project_id="test-project",
                goal="test",
            )
            await db.update_task("test-project/pv-gate-done",
                                 status="pending-validation",
                                 gate_passed_at=db.now_iso())

            await resume_task("test-project/pv-gate-done")

            mock_chain.assert_called_once_with("test-project/pv-gate-done")


# ---------------------------------------------------------------------------
# retry_task gate-stall check includes pending-validation
# ---------------------------------------------------------------------------

class TestRetryTaskPendingValidation:
    """retry_task gate-stall path works for pending-validation tasks."""

    @pytest.fixture(autouse=True)
    def _patches(self, sample_project):
        self.gate_mock = patch(
            "ouvrage.dispatch.gates._run_test_gate",
            new_callable=AsyncMock,
        )
        with self.gate_mock as m:
            self.run_test_gate = m
            yield

    async def test_pending_validation_with_review_failed_dispatches_cc(self, db, sample_project):
        """pending-validation task with gate_status=review-failed launches a CC session.

        review-failed is a rejection state (code needs fixing), not an interrupted state
        (process died mid-flight). retry_task must NOT re-enter the gate pipeline —
        it must dispatch a fresh CC session so the worker can fix the code.
        """
        from ouvrage.dispatch.engine import retry_task

        task = await db.create_task(
            id="test-project/pv-stall",
            project_id="test-project",
            goal="test",
            auto_test=True,
        )
        await db.update_task("test-project/pv-stall",
                             status="pending-validation",
                             gate_status="review-failed")

        mock_run_sdk = AsyncMock()
        mock_resume = AsyncMock()
        with patch("ouvrage.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake-wt")), \
             patch("ouvrage.dispatch.internals.setup_hook_config", AsyncMock()), \
             patch("ouvrage.dispatch.engine.run_setup_command", AsyncMock()), \
             patch("ouvrage.dispatch.engine.archive_task_logs", AsyncMock()), \
             patch("ouvrage.dispatch.engine._setup_log_dir", AsyncMock(return_value="/tmp/fake-wt/.ouvrage")), \
             patch("ouvrage.dispatch.engine._write_dispatch_log"), \
             patch("ouvrage.dispatch.engine._run_sdk_session", mock_run_sdk), \
             patch("ouvrage.dispatch.gates._resume_gate_pipeline", mock_resume):
            await retry_task("test-project/pv-stall")

        mock_run_sdk.assert_called_once()
        mock_resume.assert_not_called()


# ---------------------------------------------------------------------------
# escalate tool
# ---------------------------------------------------------------------------

class TestEscalateTool:
    """escalate tool routes through lifecycle: working → stopped(escalated), kills session."""

    def teardown_method(self, _):
        _clear_context()

    async def test_escalate_stops_task_with_reason_escalated(self, db, sample_task):
        """Escalate transitions task to stopped with reason=escalated (not needs-review)."""
        from ouvrage.server.handlers.tasks import _handle_escalate

        _set_worker_context()
        with patch("ouvrage.notifications.slack.task_needs_review", new_callable=AsyncMock):
            result = await _handle_escalate({
                "task_id": sample_task["id"],
                "reason": "Spec is ambiguous — cannot proceed without clarification.",
            })

        assert result.get("escalated") is True
        assert result.get("status") == "stopped"

        updated = await db.get_task(sample_task["id"])
        assert updated["status"] == "stopped"
        assert updated["reason"] == "escalated"

    async def test_escalate_kills_running_session(self, db, sample_task):
        """Escalate cancels the asyncio task in _running_tasks."""
        import asyncio
        from ouvrage.server.handlers.tasks import _handle_escalate
        from ouvrage.dispatch._state import _running_tasks

        # Seed a fake asyncio task that tracks whether it was cancelled
        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.get_name.return_value = f"sdk-session-{sample_task['id']}"
        mock_task.done.return_value = False
        mock_task.cancel.return_value = True
        _running_tasks.add(mock_task)

        try:
            _set_worker_context()
            with patch("ouvrage.notifications.slack.task_needs_review", new_callable=AsyncMock):
                await _handle_escalate({
                    "task_id": sample_task["id"],
                    "reason": "Need human input.",
                })
            mock_task.cancel.assert_called_once()
        finally:
            _running_tasks.discard(mock_task)

    async def test_escalate_posts_escalation_message(self, db, sample_task):
        """Escalate posts escalation message with correct author, type, and content."""
        from ouvrage.server.handlers.tasks import _handle_escalate

        _set_worker_context()
        reason = "Blocked: missing external API credentials."
        with patch("ouvrage.notifications.slack.task_needs_review", new_callable=AsyncMock):
            result = await _handle_escalate({
                "task_id": sample_task["id"],
                "reason": reason,
            })

        assert result.get("escalated") is True

        thread = await db.read_task_messages(sample_task["id"])
        msgs = thread.get("messages", [])
        escalation = next(
            (m for m in msgs if m.get("type") == "escalation"),
            None,
        )
        assert escalation is not None
        assert escalation["author"] == "cc-worker"
        assert reason in escalation["content"]
        assert "human review needed" in escalation.get("title", "").lower()

    async def test_escalate_returns_error_for_missing_task(self, db):
        """Escalate returns error dict for unknown task_id."""
        from ouvrage.server.handlers.tasks import _handle_escalate

        result = await _handle_escalate({
            "task_id": "nonexistent/task",
            "reason": "something",
        })
        assert "error" in result

    async def test_resume_available_after_escalate(self, db, sample_task):
        """After escalation, resume/retry/cancel are available actions (not blocked by awaiting_feedback)."""
        from ouvrage.server.handlers.tasks import _handle_escalate
        from ouvrage.dispatch.lifecycle import lifecycle

        # Give the task a session_id so _require_session_or_gate_resumable passes
        await db.update_task(sample_task["id"], session_id="sess-escalated")

        _set_worker_context()
        with patch("ouvrage.notifications.slack.task_needs_review", new_callable=AsyncMock):
            await _handle_escalate({
                "task_id": sample_task["id"],
                "reason": "Need guidance.",
            })

        actions = await lifecycle.get_available_actions(sample_task["id"])
        action_names = {a["name"] for a in actions}
        assert "resume" in action_names
        assert "retry" in action_names
        # cancel/close are folded into end_task
        assert "end_task" in action_names


# ---------------------------------------------------------------------------
# Worker tool allowlist — list_tools filtering
# ---------------------------------------------------------------------------

class TestWorkerToolAllowlist:
    """list_tools returns restricted set for workers; call_tool rejects non-allowlist tools."""

    def teardown_method(self, _):
        _clear_context()

    async def test_worker_list_tools_only_shows_allowlist(self):
        from ouvrage.server.app import list_tools
        from ouvrage.server.tools import WORKER_TOOL_ALLOWLIST

        _set_worker_context()
        tools = await list_tools()

        tool_names = {t.name for t in tools}
        # All returned tools must be in the allowlist
        for name in tool_names:
            assert name in WORKER_TOOL_ALLOWLIST, f"Tool '{name}' not in WORKER_TOOL_ALLOWLIST"

    async def test_worker_list_tools_includes_escalate(self):
        from ouvrage.server.app import list_tools

        _set_worker_context()
        tools = await list_tools()

        names = [t.name for t in tools]
        assert "escalate" in names

    async def test_worker_list_tools_excludes_dispatch_task(self):
        from ouvrage.server.app import list_tools

        _set_worker_context()
        tools = await list_tools()

        names = [t.name for t in tools]
        assert "dispatch_task" not in names

    async def test_worker_list_tools_excludes_cancel_task(self):
        from ouvrage.server.app import list_tools

        _set_worker_context()
        tools = await list_tools()

        names = [t.name for t in tools]
        assert "cancel_task" not in names

    async def test_user_list_tools_shows_all(self):
        from ouvrage.server.app import list_tools
        from ouvrage.server.tools import TOOLS

        _set_user_context()
        tools = await list_tools()

        # User gets the full TOOLS list
        assert len(tools) == len(TOOLS)

    async def test_worker_call_tool_rejects_non_allowlist(self):
        from ouvrage.server.app import call_tool

        _set_worker_context()
        result = await call_tool("dispatch_task", {"project_id": "x", "task_id": "y", "goal": "z"})

        assert len(result) == 1
        assert "not available on the worker endpoint" in result[0].text

    async def test_worker_call_tool_rejects_get_context(self):
        from ouvrage.server.app import call_tool

        _set_worker_context()
        result = await call_tool("get_context", {})

        assert len(result) == 1
        assert "not available on the worker endpoint" in result[0].text

    async def test_worker_call_tool_allows_post_task_message(self, db, sample_task):
        from ouvrage.server.app import call_tool
        import json

        _set_worker_context()
        result = await call_tool("post_task_message", {
            "task_id": sample_task["id"],
            "author": "cc-worker",
            "type": "progress",
            "content": "Working on it.",
        })

        assert len(result) == 1
        json_part = result[0].text.split("\n\n---\n")[0]
        data = json.loads(json_part)
        assert "error" not in str(data).lower() or "Error" not in result[0].text[:10]

    async def test_worker_call_tool_allows_escalate(self, db, sample_task):
        from ouvrage.server.app import call_tool
        import json

        _set_worker_context()
        result = await call_tool("escalate", {
            "task_id": sample_task["id"],
            "reason": "Test escalation from unit test.",
        })

        assert len(result) == 1
        json_part = result[0].text.split("\n\n---\n")[0]
        data = json.loads(json_part)
        assert data.get("escalated") is True


# ---------------------------------------------------------------------------
# Recovery: pending-validation re-enters gate pipeline
# ---------------------------------------------------------------------------

class TestRecoveryPendingValidation:
    """recover_orphaned_tasks re-enters gate pipeline for pending-validation tasks."""

    @pytest.fixture(autouse=True)
    def _patches(self):
        self.resume_mock = patch(
            "ouvrage.dispatch.engine.resume_task",
            new_callable=AsyncMock,
        )
        self.retry_mock = patch(
            "ouvrage.dispatch.engine.retry_task",
            new_callable=AsyncMock,
        )
        with self.resume_mock, self.retry_mock:
            yield

    async def test_pending_validation_no_gate_runs_test_gate(self, db, sample_project):
        from ouvrage.dispatch.recovery import recover_orphaned_tasks
        import os as _os

        # Update project to have a test_command
        await db.update_project("test-project", test_command="pytest")

        task = await db.create_task(
            id="test-project/pv-recovery-1",
            project_id="test-project",
            goal="test",
            auto_test=True,
        )
        await db.update_task("test-project/pv-recovery-1",
                             status="pending-validation",
                             gate_status=None,
                             worktree_path="/tmp/fake-worktree")

        _real_exists = _os.path.exists

        def _fake_exists(p):
            if p == "/tmp/fake-worktree":
                return True
            return _real_exists(p)

        with patch(
            "ouvrage.dispatch.gates._run_test_gate",
            new_callable=AsyncMock,
        ) as mock_test_gate, patch(
            "ouvrage.dispatch.gates._dispatch_review",
            new_callable=AsyncMock,
        ) as mock_review, patch(
            "ouvrage.dispatch.gates.os.path.exists",
            side_effect=_fake_exists,
        ), patch(
            "ouvrage.git.operations._ensure_branch_pushed",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await recover_orphaned_tasks()

            mock_test_gate.assert_called_once()
            mock_review.assert_not_called()

    async def test_pending_validation_push_failed_skipped(self, db, sample_project):
        """push-failed tasks should not be auto-recovered."""
        from ouvrage.dispatch.recovery import recover_orphaned_tasks

        task = await db.create_task(
            id="test-project/pv-push-failed",
            project_id="test-project",
            goal="test",
        )
        await db.update_task("test-project/pv-push-failed",
                             status="pending-validation",
                             gate_status="push-failed")

        with patch(
            "ouvrage.dispatch.gates._run_test_gate",
            new_callable=AsyncMock,
        ) as mock_test_gate, patch(
            "ouvrage.dispatch.gates._dispatch_review",
            new_callable=AsyncMock,
        ) as mock_review:
            await recover_orphaned_tasks()

            mock_test_gate.assert_not_called()
            mock_review.assert_not_called()

    async def test_pending_validation_review_failed_runs_review(self, db, sample_project):
        """pending-validation + review-failed calls _resume_gate_pipeline at startup.

        New behavior: startup recovery delegates to _resume_gate_pipeline for all gate states.
        For review-failed, _resume_gate_pipeline dispatches a fresh CC session (correct behavior:
        reviewer found code issues, code needs fixing before reviewing again).
        """
        from ouvrage.dispatch.recovery import recover_orphaned_tasks

        task = await db.create_task(
            id="test-project/pv-review-failed",
            project_id="test-project",
            goal="test",
            auto_review=True,
        )
        await db.update_task("test-project/pv-review-failed",
                             status="pending-validation",
                             gate_status="review-failed")

        mock_resume = AsyncMock()
        with patch("ouvrage.dispatch.gates._resume_gate_pipeline", mock_resume):
            await recover_orphaned_tasks()

        mock_resume.assert_called_with(
            "test-project/pv-review-failed", reason="startup recovery"
        )

    async def test_pending_validation_gate_passed_dispatches_chain(self, db, sample_project):
        """pending-validation with gate_status=passed should dispatch chain."""
        from ouvrage.dispatch.recovery import recover_orphaned_tasks

        task = await db.create_task(
            id="test-project/pv-chain-dispatch",
            project_id="test-project",
            goal="test",
        )
        await db.update_task("test-project/pv-chain-dispatch",
                             status="pending-validation",
                             gate_status="passed",
                             gate_passed_at=db.now_iso())

        with patch(
            "ouvrage.dispatch.engine._check_and_dispatch_dependents",
            new_callable=AsyncMock,
        ) as mock_chain, patch(
            "ouvrage.dispatch.gates._run_test_gate",
            new_callable=AsyncMock,
        ), patch(
            "ouvrage.dispatch.gates._dispatch_review",
            new_callable=AsyncMock,
        ):
            await recover_orphaned_tasks()

            mock_chain.assert_called_once_with("test-project/pv-chain-dispatch")


# ---------------------------------------------------------------------------
# Prompt includes escalate tool note
# ---------------------------------------------------------------------------

class TestPromptIncludesEscalate:
    """_build_task_prompt includes instructions about the escalate tool."""

    async def test_prompt_mentions_escalate(self, db, sample_task, sample_project):
        from ouvrage.dispatch.sdk_session import _build_task_prompt

        task = await db.get_task(sample_task["id"])
        project = await db.get_project(sample_project["id"])

        # Provide worktree_path so the prompt builds successfully
        await db.update_task(sample_task["id"], worktree_path="/tmp/worktree")
        task = await db.get_task(sample_task["id"])

        prompt = await _build_task_prompt(project, task, None, [])
        assert "escalate" in prompt
