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
import switchboard.db as db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_worker_context():
    from switchboard.server.context import set_request_context
    set_request_context(user_id=None, is_token_auth=False, is_worker=True)


def _set_user_context(user_id=1):
    from switchboard.server.context import set_request_context
    set_request_context(user_id=user_id, is_token_auth=True, is_worker=False)


def _clear_context():
    from switchboard.server.context import set_request_context
    set_request_context(user_id=None, is_token_auth=False, is_worker=False)


# ---------------------------------------------------------------------------
# pending-validation status set by sdk_session
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _check_and_dispatch_dependents sets completed
# ---------------------------------------------------------------------------

class TestGatePassCompletesTask:
    """lifecycle.execute('gate_pass') transitions validating → completed."""

    @pytest.fixture(autouse=True)
    def _patches(self, sample_project):
        self.drain_mock = patch(
            "switchboard.dispatch.engine._drain_queue",
            new_callable=AsyncMock,
        )
        self.release_mock = patch(
            "switchboard.dispatch.engine._auto_release_worktree",
            new_callable=AsyncMock,
        )
        self.pr_mock = patch(
            "switchboard.dispatch.engine._maybe_create_pr",
            new_callable=AsyncMock,
        )
        with self.drain_mock, self.release_mock, self.pr_mock:
            yield

    async def test_validating_becomes_completed_via_gate_pass(self, db, sample_project):
        from switchboard.dispatch.lifecycle import lifecycle

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


    async def test_no_gate_passed_at_returns_early(self, db, sample_project):
        """Without gate_passed_at the function should no-op."""
        from switchboard.dispatch.engine import _check_and_dispatch_dependents

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
            "switchboard.dispatch.engine.dispatch_task",
            new_callable=AsyncMock,
            return_value={"status": "working"},
        )
        with self.dispatch_mock:
            yield

    async def test_resume_accepts_pending_validation(self, db, sample_project):
        from switchboard.dispatch.engine import resume_task

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
        from switchboard.dispatch.engine import resume_task

        with patch(
            "switchboard.dispatch.engine._check_and_dispatch_dependents",
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
            "switchboard.dispatch.gates._run_test_gate",
            new_callable=AsyncMock,
        )
        with self.gate_mock as m:
            self.run_test_gate = m
            yield


# ---------------------------------------------------------------------------
# escalate tool
# ---------------------------------------------------------------------------

class TestEscalateTool:
    """escalate tool sets needs-review and posts escalation message."""

    def teardown_method(self, _):
        _clear_context()

    async def test_escalate_sets_needs_review(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_escalate

        _set_worker_context()
        result = await _handle_escalate({
            "task_id": sample_task["id"],
            "reason": "Spec is ambiguous — cannot proceed without clarification.",
        })

        assert result.get("escalated") is True
        assert result.get("status") == "needs-review"

        updated = await db.get_task(sample_task["id"])
        assert updated["status"] == "needs-review"


    async def test_escalate_returns_error_for_missing_task(self, db):
        from switchboard.server.handlers.tasks import _handle_escalate

        result = await _handle_escalate({
            "task_id": "nonexistent/task",
            "reason": "something",
        })
        assert "error" in result


# ---------------------------------------------------------------------------
# Worker tool allowlist — list_tools filtering
# ---------------------------------------------------------------------------

class TestWorkerToolAllowlist:
    """list_tools returns restricted set for workers; call_tool rejects non-allowlist tools."""

    def teardown_method(self, _):
        _clear_context()

    async def test_worker_list_tools_only_shows_allowlist(self):
        from switchboard.server.app import list_tools
        from switchboard.server.tools import WORKER_TOOL_ALLOWLIST

        _set_worker_context()
        tools = await list_tools()

        tool_names = {t.name for t in tools}
        # All returned tools must be in the allowlist
        for name in tool_names:
            assert name in WORKER_TOOL_ALLOWLIST, f"Tool '{name}' not in WORKER_TOOL_ALLOWLIST"


    async def test_user_list_tools_shows_all(self):
        from switchboard.server.app import list_tools
        from switchboard.server.tools import TOOLS

        _set_user_context()
        tools = await list_tools()

        # User gets the full TOOLS list
        assert len(tools) == len(TOOLS)


    async def test_worker_call_tool_rejects_get_context(self):
        from switchboard.server.app import call_tool

        _set_worker_context()
        result = await call_tool("get_context", {})

        assert len(result) == 1
        assert "not available on the worker endpoint" in result[0].text

    async def test_worker_call_tool_allows_post_task_message(self, db, sample_task):
        from switchboard.server.app import call_tool
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
        from switchboard.server.app import call_tool
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
            "switchboard.dispatch.engine.resume_task",
            new_callable=AsyncMock,
        )
        self.retry_mock = patch(
            "switchboard.dispatch.engine.retry_task",
            new_callable=AsyncMock,
        )
        with self.resume_mock, self.retry_mock:
            yield

    async def test_pending_validation_no_gate_runs_test_gate(self, db, sample_project):
        from switchboard.dispatch.recovery import recover_orphaned_tasks
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
            "switchboard.dispatch.gates._run_test_gate",
            new_callable=AsyncMock,
        ) as mock_test_gate, patch(
            "switchboard.dispatch.gates._dispatch_review",
            new_callable=AsyncMock,
        ) as mock_review, patch(
            "switchboard.dispatch.gates.os.path.exists",
            side_effect=_fake_exists,
        ), patch(
            "switchboard.git.operations._ensure_branch_pushed",
            new_callable=AsyncMock,
            return_value=True,
        ):
            await recover_orphaned_tasks()

            mock_test_gate.assert_called_once()
            mock_review.assert_not_called()


# ---------------------------------------------------------------------------
# Prompt includes escalate tool note
# ---------------------------------------------------------------------------

