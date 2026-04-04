"""Tier 1: Unit tests — pure logic, no real DB/git/CC.

Tests core functions by mocking database and subprocess calls.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _tail_lines — pure function, no mocks needed
# ---------------------------------------------------------------------------

class TestTailLines:
    def setup_method(self):
        from switchboard.dispatch.gates import _tail_lines
        self.fn = _tail_lines

    def test_short_text_returned_as_is(self):
        assert self.fn("hello\nworld\n", 100) == "hello\nworld\n"

    def test_truncates_at_line_boundary(self):
        text = "line1\nline2\nline3\nline4\nline5\n"
        result = self.fn(text, 18)
        # Should not start mid-line — every line should be complete
        for line in result.strip().split("\n"):
            assert len(line) > 0

    def test_empty_string(self):
        assert self.fn("", 100) == ""

    def test_single_long_line(self):
        text = "a" * 200
        result = self.fn(text, 50)
        assert len(result) <= 200

    def test_exact_boundary(self):
        text = "abc\ndef\n"
        result = self.fn(text, 8)
        assert result == text  # exactly fits


# ---------------------------------------------------------------------------
# _is_pid_alive — pure function
# ---------------------------------------------------------------------------

class TestIsPidAlive:
    def setup_method(self):
        from switchboard.dispatch.recovery import _is_pid_alive
        self.fn = _is_pid_alive

    def test_own_pid_is_alive(self):
        assert self.fn(os.getpid()) is True

    def test_bogus_pid_is_not_alive(self):
        assert self.fn(999999999) is False


# ---------------------------------------------------------------------------
# _invalidate_chain — recursive chain marking
# ---------------------------------------------------------------------------

class TestInvalidateChain:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_get_dependents = AsyncMock()
        self.mock_update_task = AsyncMock()
        self.mock_cancel_task = AsyncMock()

        patches = [
            patch("switchboard.db.get_dependents", self.mock_get_dependents),
            patch("switchboard.db.update_task", self.mock_update_task),
            patch("switchboard.db.write_audit_log", AsyncMock()),
            patch("switchboard.dispatch.engine.cancel_task", self.mock_cancel_task),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_no_dependents(self):
        from switchboard.dispatch.engine import _invalidate_chain
        self.mock_get_dependents.return_value = []
        await _invalidate_chain("task-a")
        self.mock_get_dependents.assert_awaited_once_with("task-a")
        self.mock_update_task.assert_not_awaited()

    async def test_cancels_working_tasks(self):
        from switchboard.dispatch.engine import _invalidate_chain
        self.mock_get_dependents.side_effect = [
            [{"id": "task-b", "status": "working", "gate_status": None}],
            [],  # task-b has no dependents
        ]
        await _invalidate_chain("task-a")
        self.mock_cancel_task.assert_awaited_once_with("task-b")
        # Working tasks don't match the stale condition (not in completed/ready)
        # so update_task should NOT be called for stale marking
        self.mock_update_task.assert_not_awaited()

    async def test_marks_completed_as_stale(self):
        from switchboard.dispatch.engine import _invalidate_chain
        self.mock_get_dependents.side_effect = [
            [{"id": "task-b", "status": "completed", "gate_status": "passed"}],
            [],
        ]
        await _invalidate_chain("task-a")
        self.mock_cancel_task.assert_not_awaited()
        self.mock_update_task.assert_awaited_once_with(
            "task-b", gate_status="stale", gate_passed_at=None
        )

    async def test_recursive_chain(self):
        """A -> B -> C: invalidating A should mark both B and C stale."""
        from switchboard.dispatch.engine import _invalidate_chain
        self.mock_get_dependents.side_effect = [
            [{"id": "task-b", "status": "completed", "gate_status": "passed"}],
            [{"id": "task-c", "status": "ready", "gate_status": None}],
            [],  # task-c has no dependents
        ]
        await _invalidate_chain("task-a")
        # B marked stale (completed with passed gate)
        assert any(
            call.args == ("task-b",) and call.kwargs.get("gate_status") == "stale"
            for call in self.mock_update_task.await_args_list
        )
        # C marked stale (ready status)
        assert any(
            call.args == ("task-c",) and call.kwargs.get("gate_status") == "stale"
            for call in self.mock_update_task.await_args_list
        )

    async def test_skips_already_stale(self):
        """Already-stale task with cancelled status — not in the marking condition."""
        from switchboard.dispatch.engine import _invalidate_chain
        self.mock_get_dependents.side_effect = [
            [{"id": "task-b", "status": "cancelled", "gate_status": "stale"}],
            [],
        ]
        await _invalidate_chain("task-a")
        # get_dependents called twice (task-a, task-b) — recurses regardless
        assert self.mock_get_dependents.await_count == 2
        # cancelled + stale doesn't match the marking condition
        self.mock_update_task.assert_not_awaited()


# ---------------------------------------------------------------------------
# _process_review_result_inline — review verdict handling
# ---------------------------------------------------------------------------

class TestProcessReviewResultInline:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_read_msgs = AsyncMock()
        self.mock_update_task = AsyncMock()
        self.mock_get_task = AsyncMock()
        self.mock_check_deps = AsyncMock()
        self.mock_lifecycle_execute = AsyncMock()
        self.mock_notify = AsyncMock()

        patches = [
            patch("switchboard.db.read_task_messages", self.mock_read_msgs),
            patch("switchboard.db.update_task", self.mock_update_task),
            patch("switchboard.db.get_task", self.mock_get_task),
            patch("switchboard.db.write_audit_log", AsyncMock()),
            patch("switchboard.dispatch.engine._check_and_dispatch_dependents", self.mock_check_deps),
            patch("switchboard.dispatch.lifecycle.lifecycle.execute", self.mock_lifecycle_execute),
            patch("switchboard.notifications.slack.task_needs_review", self.mock_notify),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_approved_passes_gate(self):
        from switchboard.dispatch.gates import _process_review_result_inline
        self.mock_read_msgs.return_value = {
            "messages": [
                {"type": "review", "title": "APPROVED", "content": "Looks good"},
            ]
        }
        await _process_review_result_inline("task-1")
        # Should call lifecycle gate_pass (handles status + deps as side effects)
        self.mock_lifecycle_execute.assert_awaited_once()
        call_args = self.mock_lifecycle_execute.await_args
        assert call_args[0] == ("task-1", "gate_pass")

    async def test_rejected_retries_if_under_limit(self):
        from switchboard.dispatch.gates import _process_review_result_inline
        self.mock_read_msgs.return_value = {
            "messages": [
                {"type": "review", "title": "CHANGES REQUESTED", "content": "Fix X"},
            ]
        }
        self.mock_get_task.return_value = {
            "gate_retries": 0,
            "max_gate_retries": 3,
        }
        await _process_review_result_inline("task-1")
        self.mock_lifecycle_execute.assert_awaited_once()
        call_args = self.mock_lifecycle_execute.await_args
        assert call_args[0] == ("task-1", "retry")

    async def test_rejected_escalates_after_max_retries(self):
        from switchboard.dispatch.gates import _process_review_result_inline
        self.mock_read_msgs.return_value = {
            "messages": [
                {"type": "review", "title": "CHANGES REQUESTED", "content": "Still broken"},
            ]
        }
        self.mock_get_task.return_value = {
            "id": "task-1",
            "goal": "test",
            "gate_retries": 2,
            "max_gate_retries": 3,
        }
        await _process_review_result_inline("task-1")
        # Should call lifecycle gate_fail (max review retries exceeded)
        assert any(
            call[0] == ("task-1", "gate_fail")
            for call in self.mock_lifecycle_execute.await_args_list
        )

    async def test_no_review_message_goes_to_rejection_path(self):
        """No review message = falls to else branch (rejection)."""
        from switchboard.dispatch.gates import _process_review_result_inline
        self.mock_read_msgs.return_value = {"messages": []}
        self.mock_get_task.return_value = {
            "id": "task-1",
            "goal": "test",
            "gate_retries": 0,
            "max_gate_retries": 3,
        }
        await _process_review_result_inline("task-1")
        # With no review message, review_msg is None, condition fails → rejection path
        self.mock_lifecycle_execute.assert_awaited_once()
        call_args = self.mock_lifecycle_execute.await_args
        assert call_args[0] == ("task-1", "retry")

    async def test_not_approved_title_does_not_pass_gate(self):
        """'NOT APPROVED' must not trigger approval — exact match only."""
        from switchboard.dispatch.gates import _process_review_result_inline
        self.mock_read_msgs.return_value = {
            "messages": [
                {"type": "review", "title": "NOT APPROVED", "content": "Issues found"},
            ]
        }
        self.mock_get_task.return_value = {
            "id": "task-1",
            "goal": "test",
            "gate_retries": 0,
            "max_gate_retries": 3,
        }
        await _process_review_result_inline("task-1")
        # Must NOT pass the gate
        assert not any(
            call.kwargs.get("gate_status") == "passed"
            for call in self.mock_update_task.await_args_list
        )
        self.mock_check_deps.assert_not_awaited()


# ---------------------------------------------------------------------------
# _check_and_dispatch_dependents — routing logic
# ---------------------------------------------------------------------------

class TestCheckAndDispatchDependents:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_get_task = AsyncMock()
        self.mock_get_dependents = AsyncMock()
        self.mock_lifecycle_execute = AsyncMock()
        self.mock_rebase = AsyncMock()
        self.mock_pr = AsyncMock()
        self.mock_drain = AsyncMock()
        self.mock_auto_merge = AsyncMock(return_value=True)
        self.mock_auto_release = AsyncMock()
        self.mock_resolve_punchlist = AsyncMock(return_value=0)
        self.mock_post_msg = AsyncMock()
        self.mock_update_task = AsyncMock()

        patches = [
            patch("switchboard.db.get_task", self.mock_get_task),
            patch("switchboard.db.get_dependents", self.mock_get_dependents),
            patch("switchboard.db.update_task", self.mock_update_task),
            patch("switchboard.db.write_audit_log", AsyncMock()),
            patch("switchboard.dispatch.lifecycle.lifecycle.execute", self.mock_lifecycle_execute),
            patch("switchboard.dispatch.engine._rebase_and_redispatch", self.mock_rebase),
            patch("switchboard.dispatch.engine._maybe_create_pr", self.mock_pr),
            patch("switchboard.dispatch.engine._drain_queue", self.mock_drain),
            patch("switchboard.dispatch.engine._perform_auto_merge", self.mock_auto_merge),
            patch("switchboard.dispatch.engine._auto_release_worktree", self.mock_auto_release),
            patch("switchboard.db.resolve_punchlist_items_for_task", self.mock_resolve_punchlist),
            patch("switchboard.db.post_task_message", self.mock_post_msg),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_dispatches_ready_dependents(self):
        from switchboard.dispatch.engine import _check_and_dispatch_dependents
        self.mock_get_task.return_value = {
            "id": "task-a", "project_id": "proj", "gate_passed_at": "2026-01-01",
            "auto_test": True,
        }
        self.mock_get_dependents.return_value = [
            {"id": "task-b", "status": "ready", "gate_status": None,
             "project_id": "proj", "goal": "do B"},
        ]
        await _check_and_dispatch_dependents("task-a")
        self.mock_lifecycle_execute.assert_awaited_once()
        call_args = self.mock_lifecycle_execute.await_args[0]
        assert call_args[0] == "task-b"
        assert call_args[1] == "dispatch"

    async def test_rebases_stale_completed(self):
        from switchboard.dispatch.engine import _check_and_dispatch_dependents
        parent = {
            "id": "task-a", "project_id": "proj", "gate_passed_at": "2026-01-01",
            "auto_test": True,
        }
        self.mock_get_task.return_value = parent
        dep = {
            "id": "task-b", "status": "completed", "gate_status": "stale",
            "project_id": "proj", "goal": "do B",
        }
        self.mock_get_dependents.return_value = [dep]
        await _check_and_dispatch_dependents("task-a")
        self.mock_rebase.assert_awaited_once_with(dep, parent)

    async def test_no_dispatch_if_gate_not_passed(self):
        from switchboard.dispatch.engine import _check_and_dispatch_dependents
        self.mock_get_task.return_value = {
            "id": "task-a", "gate_passed_at": None,
        }
        await _check_and_dispatch_dependents("task-a")
        self.mock_get_dependents.assert_not_awaited()

    async def test_creates_pr_when_no_dependents(self):
        from switchboard.dispatch.engine import _check_and_dispatch_dependents
        self.mock_get_task.return_value = {
            "id": "task-a", "project_id": "proj", "gate_passed_at": "2026-01-01",
        }
        self.mock_get_dependents.return_value = []
        await _check_and_dispatch_dependents("task-a")
        self.mock_pr.assert_awaited_once_with("task-a")

    async def test_held_task_skips_dispatch(self):
        """Fix 1 regression: held tasks must NOT be dispatched."""
        from switchboard.dispatch.engine import _check_and_dispatch_dependents
        self.mock_get_task.return_value = {
            "id": "task-a", "project_id": "proj", "gate_passed_at": "2026-01-01",
        }
        self.mock_get_dependents.return_value = [
            {"id": "task-b", "status": "ready", "held": True,
             "project_id": "proj", "goal": "do B"},
        ]
        await _check_and_dispatch_dependents("task-a")
        self.mock_lifecycle_execute.assert_not_awaited()

    async def test_non_held_ready_task_actually_dispatches(self):
        """Fix 1: non-held ready dependent task calls lifecycle.execute('dispatch')."""
        from switchboard.dispatch.engine import _check_and_dispatch_dependents
        self.mock_get_task.return_value = {
            "id": "task-a", "project_id": "proj", "gate_passed_at": "2026-01-01",
        }
        self.mock_get_dependents.return_value = [
            {"id": "task-b", "status": "ready", "held": False,
             "project_id": "proj", "goal": "do B", "auto_test": True},
        ]
        await _check_and_dispatch_dependents("task-a")
        self.mock_lifecycle_execute.assert_awaited_once()
        call_args = self.mock_lifecycle_execute.await_args[0]
        assert call_args[0] == "task-b"
        assert call_args[1] == "dispatch"

    async def test_mixed_held_and_non_held(self):
        """Fix 1: only the non-held ready task dispatches; held one is skipped."""
        from switchboard.dispatch.engine import _check_and_dispatch_dependents
        self.mock_get_task.return_value = {
            "id": "task-a", "project_id": "proj", "gate_passed_at": "2026-01-01",
        }
        self.mock_get_dependents.return_value = [
            {"id": "task-b", "status": "ready", "held": False,
             "project_id": "proj", "goal": "do B", "auto_test": True},
            {"id": "task-c", "status": "ready", "held": True,
             "project_id": "proj", "goal": "do C"},
        ]
        await _check_and_dispatch_dependents("task-a")
        self.mock_lifecycle_execute.assert_awaited_once()
        call_args = self.mock_lifecycle_execute.await_args[0]
        assert call_args[0] == "task-b"
        assert call_args[1] == "dispatch"


# ---------------------------------------------------------------------------
# _maybe_create_pr — auto-PR on chain tail
# ---------------------------------------------------------------------------

class TestMaybeCreatePr:
    """_maybe_create_pr must actually fire when task is the chain tail."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_create_pr = AsyncMock(return_value={"url": "https://github.com/acme/widgets/pull/42", "number": 42})
        self.mock_get_pat = AsyncMock(return_value="ghp_fake_token_123")
        self.mock_add_artifact = AsyncMock()
        self.mock_post_msg = AsyncMock()

        patches = [
            patch("switchboard.git.operations.create_github_pr", self.mock_create_pr),
            patch("switchboard.git.operations.get_github_pat", self.mock_get_pat),
            patch("switchboard.git.operations.db.add_artifact", self.mock_add_artifact),
            patch("switchboard.git.operations.db.post_task_message", self.mock_post_msg),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_pr_created_when_worktree_exists(self, db, sample_project):
        """auto_pr task with worktree should create a PR via REST API."""
        from switchboard.git.operations import _maybe_create_pr

        task = await db.create_task(
            id="test-project/pr-tail", project_id="test-project",
            goal="Build the thing", auto_pr=True,
        )
        await db.update_task(task["id"],
            status="completed", gate_status="passed", gate_passed_at=db.now_iso(),
            worktree_path="/tmp/fake-worktree", branch="pr-tail",
        )

        await _maybe_create_pr("test-project/pr-tail")
        self.mock_create_pr.assert_awaited_once()
        # Verify REST API was called with correct owner/repo parsed from SSH URL
        call_kwargs = self.mock_create_pr.await_args.kwargs
        assert call_kwargs["owner"] == "acme"
        assert call_kwargs["repo"] == "widgets"
        assert call_kwargs["head"] == "pr-tail"
        assert call_kwargs["base"] == "main"

    async def test_pr_skipped_when_no_worktree(self, db, sample_project):
        """auto_pr task WITHOUT worktree should silently skip."""
        from switchboard.git.operations import _maybe_create_pr

        task = await db.create_task(
            id="test-project/pr-no-wt", project_id="test-project",
            goal="Build the thing", auto_pr=True,
        )
        await db.update_task(task["id"],
            status="completed", gate_status="passed", gate_passed_at=db.now_iso(),
            worktree_path=None, branch="pr-no-wt",
        )

        await _maybe_create_pr("test-project/pr-no-wt")
        self.mock_create_pr.assert_not_awaited()

    async def test_pr_targets_task_base_branch_when_set(self, db, sample_project):
        """PR base must be task.base_branch, not project default_branch."""
        from switchboard.git.operations import _maybe_create_pr

        task = await db.create_task(
            id="test-project/pr-base-override", project_id="test-project",
            goal="Feature on saas branch", auto_pr=True,
            base_branch="foreman-saas",
        )
        await db.update_task(task["id"],
            status="completed", gate_status="passed", gate_passed_at=db.now_iso(),
            worktree_path="/tmp/fake-worktree", branch="pr-base-override",
        )

        await _maybe_create_pr("test-project/pr-base-override")
        self.mock_create_pr.assert_awaited_once()
        call_kwargs = self.mock_create_pr.await_args.kwargs
        assert call_kwargs["base"] == "foreman-saas"

    async def test_pr_falls_back_to_default_branch_when_base_branch_null(self, db, sample_project):
        """PR base falls back to project.default_branch when task.base_branch is null."""
        from switchboard.git.operations import _maybe_create_pr

        task = await db.create_task(
            id="test-project/pr-default-branch", project_id="test-project",
            goal="Feature on default branch", auto_pr=True,
            base_branch=None,
        )
        await db.update_task(task["id"],
            status="completed", gate_status="passed", gate_passed_at=db.now_iso(),
            worktree_path="/tmp/fake-worktree", branch="pr-default-branch",
        )

        await _maybe_create_pr("test-project/pr-default-branch")
        self.mock_create_pr.assert_awaited_once()
        call_kwargs = self.mock_create_pr.await_args.kwargs
        assert call_kwargs["base"] == "main"  # sample_project.default_branch

    async def test_auto_release_before_pr_causes_silent_skip(self, db, sample_project):
        """Integration: _check_and_dispatch_dependents releases worktree before PR creation.

        This is the actual bug: auto_release clears worktree_path from DB,
        then _maybe_create_pr sees worktree_path=None and bails.
        The PR is never created despite auto_pr=true.
        """
        from switchboard.dispatch.engine import _check_and_dispatch_dependents

        task = await db.create_task(
            id="test-project/pr-release-bug", project_id="test-project",
            goal="Chain tail", auto_pr=True, auto_release_worktree=True,
        )
        await db.update_task(task["id"],
            status="completed", gate_status="passed", gate_passed_at=db.now_iso(),
            worktree_path="/tmp/fake-worktree", branch="pr-release-bug",
        )

        # Mock release_worktree to simulate what the real one does: clear worktree_path
        async def fake_release(tid, reason="detach"):
            await db.update_task(tid, worktree_path=None)
            return {"released": True}

        with patch("switchboard.dispatch.engine.release_worktree", AsyncMock(side_effect=fake_release)):
            with patch("switchboard.dispatch.engine._drain_queue", AsyncMock()):
                with patch("switchboard.db.resolve_punchlist_items_for_task", AsyncMock(return_value=0)):
                    await _check_and_dispatch_dependents("test-project/pr-release-bug")

        # The PR should have been created
        self.mock_create_pr.assert_awaited_once()


# ---------------------------------------------------------------------------
# held + depends_on interaction — hold must persist across dependency wait
# ---------------------------------------------------------------------------

class TestHeldWithDependsOn:
    """Bug: held=True dropped when depends_on parent hasn't gate-passed yet.

    dispatch_task returns early for pending dependencies BEFORE persisting the
    held flag.  When the parent later gate-passes, _check_and_dispatch_dependents
    sees held=False and auto-dispatches — defeating the hold.
    """

    async def test_held_flag_persisted_despite_pending_dependency(self, db, sample_project):
        """held=True must be saved to DB even when depends_on causes early return."""
        from switchboard.dispatch.engine import dispatch_task

        # Create the parent task (not yet gate-passed)
        await db.create_task(
            id="test-project/parent-task", project_id="test-project",
            goal="Parent task",
        )

        # Dispatch child with held=True + depends_on parent
        result = await dispatch_task(
            project_id="test-project",
            task_id="test-project/held-child",
            goal="Child that should be held",
            held=True,
            depends_on="test-project/parent-task",
        )

        # Should return early because parent hasn't gate-passed
        assert result["status"] == "ready"
        assert "waiting_on" in result or result.get("held") is True

        # The critical check: held flag must be persisted in DB
        task = await db.get_task("test-project/held-child")
        assert task["held"], (
            "held=True was not persisted to DB — it will be ignored when "
            "dependency resolves and the task will auto-dispatch"
        )

    async def test_held_task_not_auto_dispatched_on_dependency_resolution(self, db, sample_project):
        """When parent gate-passes, held dependent must NOT auto-dispatch."""
        from switchboard.dispatch.engine import _check_and_dispatch_dependents

        # Create parent task that has gate-passed
        parent = await db.create_task(
            id="test-project/gated-parent", project_id="test-project",
            goal="Parent",
        )
        await db.update_task(parent["id"],
            status="completed", gate_status="passed",
            gate_passed_at=db.now_iso(),
        )

        # Create child task with held=True and depends_on parent
        child = await db.create_task(
            id="test-project/held-dep-child", project_id="test-project",
            goal="Held child", depends_on="test-project/gated-parent",
        )
        await db.update_task(child["id"], held=True)

        # Now run dependency resolution
        with patch("switchboard.dispatch.engine.release_worktree", AsyncMock()):
            with patch("switchboard.dispatch.engine._drain_queue", AsyncMock()):
                with patch("switchboard.dispatch.engine.dispatch_task", AsyncMock()) as mock_dispatch:
                    with patch("switchboard.db.resolve_punchlist_items_for_task", AsyncMock(return_value=0)):
                        await _check_and_dispatch_dependents("test-project/gated-parent")

        # dispatch_task must NOT have been called for the held child
        mock_dispatch.assert_not_awaited()

        # Task should still be held
        task = await db.get_task("test-project/held-dep-child")
        assert task["held"], "held flag was cleared during dependency resolution"


# ---------------------------------------------------------------------------
# check_stalled_tasks — stall detection and orphan recovery logic
# ---------------------------------------------------------------------------

class TestCheckStalledTasksRouting:
    """Fix 3: stall detection runs for active-client tasks; orphan recovery for no-client tasks."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_list_tasks = AsyncMock(return_value=[])
        self.mock_get_task = AsyncMock()
        self.mock_update_task = AsyncMock()
        self.mock_post_msg = AsyncMock()
        self.mock_read_msgs = AsyncMock(return_value={"messages": []})
        self.mock_recover = AsyncMock()
        self.mock_notify = AsyncMock()
        self.mock_notify.task_heartbeat = AsyncMock()
        self.mock_sleep = AsyncMock()

        patches = [
            patch("switchboard.db.list_tasks", self.mock_list_tasks),
            patch("switchboard.db.get_task", self.mock_get_task),
            patch("switchboard.db.update_task", self.mock_update_task),
            patch("switchboard.db.post_task_message", self.mock_post_msg),
            patch("switchboard.db.read_task_messages", self.mock_read_msgs),
            patch("switchboard.db.get_project", AsyncMock(return_value=None)),
            patch("switchboard.db.get_component", AsyncMock(return_value=None)),
            patch("switchboard.dispatch.recovery._recover_single_task", self.mock_recover),
            patch("switchboard.dispatch.recovery.notify", self.mock_notify),
            patch("switchboard.dispatch.recovery.asyncio.sleep", self.mock_sleep),
            patch("switchboard.dispatch.engine.retry_task", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    def _make_task(self, task_id, idle_seconds, has_active_client):
        from datetime import datetime, timezone, timedelta
        last = (datetime.now(timezone.utc) - timedelta(seconds=idle_seconds)).isoformat()
        from switchboard.dispatch._state import _active_clients
        if has_active_client:
            _active_clients[task_id] = object()
        return {"id": task_id, "status": "working", "last_activity": last}

    def teardown_method(self):
        from switchboard.dispatch._state import _active_clients
        _active_clients.clear()

    async def test_stall_warning_fires_for_active_client_task(self):
        """Fix 3: stall warning posts when active-client task is idle >=300s."""
        from switchboard.dispatch.recovery import check_stalled_tasks
        task = self._make_task("proj/stalled-1", idle_seconds=310, has_active_client=True)
        self.mock_list_tasks.side_effect = lambda status=None: (
            [task] if status == "working" else []
        )
        import asyncio as _asyncio
        self.mock_sleep.side_effect = [None, _asyncio.CancelledError()]
        try:
            await check_stalled_tasks()
        except _asyncio.CancelledError:
            pass
        self.mock_post_msg.assert_awaited()
        calls = [c for c in self.mock_post_msg.await_args_list
                 if c.kwargs.get("type") == "stall-warning"]
        assert len(calls) == 1

    async def test_no_stall_warning_for_active_client_task_below_threshold(self):
        """Fix 3: no stall warning when active-client task is idle <300s."""
        from switchboard.dispatch.recovery import check_stalled_tasks
        task = self._make_task("proj/fresh-1", idle_seconds=60, has_active_client=True)
        self.mock_list_tasks.side_effect = lambda status=None: (
            [task] if status == "working" else []
        )
        import asyncio as _asyncio
        self.mock_sleep.side_effect = [None, _asyncio.CancelledError()]
        try:
            await check_stalled_tasks()
        except _asyncio.CancelledError:
            pass
        stall_calls = [c for c in self.mock_post_msg.await_args_list
                       if c.kwargs.get("type") == "stall-warning"]
        assert len(stall_calls) == 0

    async def test_orphan_recovery_for_no_client_task(self):
        """Fix 3: orphan recovery triggers for no-client task idle >120s."""
        from switchboard.dispatch.recovery import check_stalled_tasks
        task = self._make_task("proj/orphan-1", idle_seconds=200, has_active_client=False)
        task_obj = dict(task, session_id="s1")
        self.mock_get_task.return_value = task_obj
        self.mock_list_tasks.side_effect = lambda status=None: (
            [task] if status == "working" else []
        )
        import asyncio as _asyncio
        self.mock_sleep.side_effect = [None, _asyncio.CancelledError()]
        try:
            await check_stalled_tasks()
        except _asyncio.CancelledError:
            pass
        self.mock_recover.assert_awaited_once()

    async def test_no_recovery_for_no_client_task_below_orphan_threshold(self):
        """Fix 3: no recovery for no-client task idle <=120s (not dead yet)."""
        from switchboard.dispatch.recovery import check_stalled_tasks
        task = self._make_task("proj/recent-1", idle_seconds=30, has_active_client=False)
        self.mock_list_tasks.side_effect = lambda status=None: (
            [task] if status == "working" else []
        )
        import asyncio as _asyncio
        self.mock_sleep.side_effect = [None, _asyncio.CancelledError()]
        try:
            await check_stalled_tasks()
        except _asyncio.CancelledError:
            pass
        self.mock_recover.assert_not_awaited()

    async def test_stall_not_triggered_for_no_client_task(self):
        """Fix 3: stall warning does NOT fire for no-client tasks (even if idle >300s)."""
        from switchboard.dispatch.recovery import check_stalled_tasks
        task = self._make_task("proj/orphan-stale", idle_seconds=400, has_active_client=False)
        task_obj = dict(task, session_id="s1")
        self.mock_get_task.return_value = task_obj
        self.mock_list_tasks.side_effect = lambda status=None: (
            [task] if status == "working" else []
        )
        import asyncio as _asyncio
        self.mock_sleep.side_effect = [None, _asyncio.CancelledError()]
        try:
            await check_stalled_tasks()
        except _asyncio.CancelledError:
            pass
        stall_calls = [c for c in self.mock_post_msg.await_args_list
                       if c.kwargs.get("type") == "stall-warning"]
        assert len(stall_calls) == 0


# ---------------------------------------------------------------------------
# check_stalled_tasks — chain advancement recovery respects held flag
# ---------------------------------------------------------------------------

class TestCheckStalledTasksHeldChain:
    """Recovery sweep must NOT dispatch held tasks even when parent has gate-passed."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_list_tasks = AsyncMock(return_value=[])
        self.mock_get_task = AsyncMock()
        self.mock_update_task = AsyncMock()
        self.mock_post_msg = AsyncMock()
        self.mock_read_msgs = AsyncMock(return_value={"messages": []})
        self.mock_recover = AsyncMock()
        self.mock_notify = AsyncMock()
        self.mock_notify.task_heartbeat = AsyncMock()
        self.mock_sleep = AsyncMock()
        self.mock_retry = AsyncMock()
        self.mock_get_project = AsyncMock(return_value=None)
        self.mock_get_component = AsyncMock(return_value=None)

        patches = [
            patch("switchboard.db.list_tasks", self.mock_list_tasks),
            patch("switchboard.db.get_task", self.mock_get_task),
            patch("switchboard.db.update_task", self.mock_update_task),
            patch("switchboard.db.post_task_message", self.mock_post_msg),
            patch("switchboard.db.read_task_messages", self.mock_read_msgs),
            patch("switchboard.db.get_project", self.mock_get_project),
            patch("switchboard.db.get_component", self.mock_get_component),
            patch("switchboard.dispatch.recovery._recover_single_task", self.mock_recover),
            patch("switchboard.dispatch.recovery.notify", self.mock_notify),
            patch("switchboard.dispatch.recovery.asyncio.sleep", self.mock_sleep),
            # retry_task is imported locally in recovery.py — patch at the source module
            patch("switchboard.dispatch.engine.retry_task", self.mock_retry),
        ]
        started = []
        try:
            for p in patches:
                p.start()
                started.append(p)
        except Exception:
            for p in started:
                p.stop()
            raise
        yield
        for p in started:
            p.stop()

    async def test_held_child_skipped_when_parent_gate_passed(self):
        """Recovery sweep must NOT dispatch a held child even when parent gate-passed."""
        from switchboard.dispatch.recovery import check_stalled_tasks

        parent = {"id": "proj/parent", "gate_passed_at": "2026-01-01", "auto_merge": None, "pr_status": None}
        child = {
            "id": "proj/child", "status": "ready", "depends_on": "proj/parent",
            "project_id": "proj", "component_id": None, "held": True,
        }

        self.mock_list_tasks.side_effect = lambda status=None: (
            [child] if status == "ready" else []
        )
        self.mock_get_task.return_value = parent

        import asyncio as _asyncio
        self.mock_sleep.side_effect = [None, _asyncio.CancelledError()]
        try:
            await check_stalled_tasks()
        except _asyncio.CancelledError:
            pass

        self.mock_retry.assert_not_awaited()

    async def test_non_held_child_dispatched_when_parent_gate_passed(self):
        """Recovery sweep MUST dispatch a non-held child when parent gate-passed."""
        from switchboard.dispatch.recovery import check_stalled_tasks

        parent = {"id": "proj/parent", "gate_passed_at": "2026-01-01", "auto_merge": None, "pr_status": None}
        child = {
            "id": "proj/child", "status": "ready", "depends_on": "proj/parent",
            "project_id": "proj", "component_id": None, "held": False,
        }

        self.mock_list_tasks.side_effect = lambda status=None: (
            [child] if status == "ready" else []
        )
        self.mock_get_task.return_value = parent

        import asyncio as _asyncio
        self.mock_sleep.side_effect = [None, _asyncio.CancelledError()]
        try:
            await check_stalled_tasks()
        except _asyncio.CancelledError:
            pass

        self.mock_retry.assert_awaited_once_with("proj/child")


# ---------------------------------------------------------------------------
# approve_task — held child dispatches after parent gate-passed
# ---------------------------------------------------------------------------

class TestApproveHeldChainChild:
    """approve_task on a held child whose parent already gate-passed must dispatch it."""

    async def test_approve_held_child_dispatches_when_parent_passed(self, db, sample_project, mock_git, mock_sdk):
        """After parent gate-passes, approving the held child must trigger dispatch."""
        from switchboard.dispatch.engine import approve_task

        # Create parent task that has gate-passed
        await db.create_task(
            id="test-project/approve-parent", project_id="test-project",
            goal="Parent",
        )
        await db.update_task("test-project/approve-parent",
            status="completed", gate_status="passed",
            gate_passed_at=db.now_iso(),
        )

        # Create held child task
        await db.create_task(
            id="test-project/approve-child", project_id="test-project",
            goal="Child to approve", depends_on="test-project/approve-parent",
        )
        await db.update_task("test-project/approve-child", held=True)

        result = await approve_task("test-project/approve-child")

        # held flag must be cleared in DB
        task = await db.get_task("test-project/approve-child")
        assert not task["held"], "held flag should be cleared after approval"

        # Result must reflect the dispatch outcome — not an error, not re-held
        assert result.get("status") == "working", (
            "approve_task must dispatch and return status=working, "
            f"got: {result}"
        )
        assert result.get("held") is not True, "response must not indicate task is still held"


# ---------------------------------------------------------------------------
# approve_task — response correctness (no re-validation after mutation)
# ---------------------------------------------------------------------------

class TestApproveTaskResponse:
    """approve_task must return success after approving, never a re-validation error."""

    async def test_approve_standalone_held_task_returns_working(self, db, sample_project, mock_git, mock_sdk):
        """Approving a standalone held task returns status=working, no error."""
        from switchboard.dispatch.engine import approve_task

        await db.create_task(
            id="test-project/standalone-held", project_id="test-project",
            goal="Do the thing",
        )
        await db.update_task("test-project/standalone-held", held=True)

        result = await approve_task("test-project/standalone-held")

        # Response must reflect the dispatch result — no "is not held" error
        assert result.get("status") == "working", (
            f"Expected status=working, got: {result}"
        )
        assert result.get("held") is not True, "Response must not say task is still held"

        # DB must have held=False
        task = await db.get_task("test-project/standalone-held")
        assert not task["held"]

    async def test_approve_non_held_task_raises_not_held(self, db, sample_project):
        """Approving a task that is not held raises ValueError with 'is not held'."""
        from switchboard.dispatch.engine import approve_task

        await db.create_task(
            id="test-project/not-held", project_id="test-project",
            goal="Already running, not held",
        )
        # held defaults to False — do not set it

        with pytest.raises(ValueError, match="is not held"):
            await approve_task("test-project/not-held")

    async def test_rapid_double_approve_first_succeeds_second_clean_error(self, db, sample_project, mock_git, mock_sdk):
        """First approve succeeds; second approve gets a clean ValueError, not a crash."""
        from switchboard.dispatch.engine import approve_task

        await db.create_task(
            id="test-project/double-approve", project_id="test-project",
            goal="Approve me twice",
        )
        await db.update_task("test-project/double-approve", held=True)

        # First approve — succeeds
        result = await approve_task("test-project/double-approve")
        assert result.get("status") == "working"

        # Second approve — must raise clean ValueError (task is now working, not held)
        # IllegalTransition extends ValueError, so this covers both cases
        with pytest.raises(ValueError):
            await approve_task("test-project/double-approve")


# ---------------------------------------------------------------------------
# _ensure_branch_pushed — git push logic
# ---------------------------------------------------------------------------

class TestEnsureBranchPushed:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_run = AsyncMock()
        self.mock_post_msg = AsyncMock()
        self.mock_exists = lambda p: True  # worktree always exists in tests
        self.mock_resolve_url = AsyncMock(return_value="https://oauth2:ghp_test@github.com/acme/widgets.git")

        patches = [
            patch("switchboard.git.operations._run_as_worker", self.mock_run),
            patch("switchboard.git.operations.db.post_task_message", self.mock_post_msg),
            patch("switchboard.git.operations.os.path.exists", side_effect=self.mock_exists),
            patch("switchboard.git.operations._resolve_push_url", self.mock_resolve_url),
        ]
        for p in patches:
            p.start()
        self._patches = patches
        yield
        for p in patches:
            p.stop()

    async def test_no_worktree_noop(self):
        from switchboard.git.operations import _ensure_branch_pushed
        await _ensure_branch_pushed("t1", {"worktree_path": None, "branch": "feat", "project_id": "p"})
        self.mock_run.assert_not_awaited()

    async def test_no_branch_noop(self):
        from switchboard.git.operations import _ensure_branch_pushed
        await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": None, "project_id": "p"})
        self.mock_run.assert_not_awaited()

    async def test_no_worktree_returns_true(self):
        from switchboard.git.operations import _ensure_branch_pushed
        result = await _ensure_branch_pushed("t1", {"worktree_path": None, "branch": "feat", "project_id": "p"})
        assert result is True
        self.mock_run.assert_not_awaited()

    async def test_no_branch_returns_true(self):
        from switchboard.git.operations import _ensure_branch_pushed
        result = await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": None, "project_id": "p"})
        assert result is True
        self.mock_run.assert_not_awaited()

    async def test_nothing_to_push(self):
        from switchboard.git.operations import _ensure_branch_pushed
        # credential helper check + ls-remote returns a ref (remote exists) + log shows nothing unpushed
        self.mock_run.side_effect = [
            (b"", b"", 0),  # git config credential.helper — no cred helper
            (b"abc123\trefs/heads/feat\n", b"", 0),  # ls-remote
            (b"", b"", 0),  # log shows nothing unpushed
        ]
        result = await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": "feat", "project_id": "p"})
        assert result is True
        assert self.mock_run.await_count == 3  # config + ls-remote + log, no push

    async def test_pushes_unpushed_commits(self):
        from switchboard.git.operations import _ensure_branch_pushed
        self.mock_run.side_effect = [
            (b"", b"", 0),  # git config credential.helper — no cred helper
            (b"abc123\trefs/heads/feat\n", b"", 0),  # ls-remote
            (b"abc Fix something\n", b"", 0),  # log shows unpushed
            (b"", b"", 0),  # push succeeds
        ]
        result = await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": "feat", "project_id": "p"})
        assert result is True
        assert self.mock_run.await_count == 4
        push_call = self.mock_run.await_args_list[3]
        assert "push" in push_call.args
        assert "--force" in push_call.args
        assert "--force-with-lease" not in push_call.args

    async def test_pushes_when_no_remote_branch(self):
        from switchboard.git.operations import _ensure_branch_pushed
        self.mock_run.side_effect = [
            (b"", b"", 0),  # git config credential.helper — no cred helper
            (b"", b"", 0),  # ls-remote returns empty (no remote branch)
            (b"", b"", 0),  # push succeeds
        ]
        result = await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": "feat", "project_id": "p"})
        assert result is True
        assert self.mock_run.await_count == 3
        push_call = self.mock_run.await_args_list[2]
        assert "push" in push_call.args

    async def test_push_failure_returns_false(self):
        from switchboard.git.operations import _ensure_branch_pushed
        self.mock_run.side_effect = [
            (b"", b"", 0),  # git config credential.helper — no cred helper
            (b"", b"", 0),  # ls-remote empty (no remote branch)
            (b"", b"rejected", 1),  # push fails
        ]
        result = await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": "feat", "project_id": "p"})
        assert result is False

    async def test_push_failure_posts_message(self):
        from switchboard.git.operations import _ensure_branch_pushed
        self.mock_run.side_effect = [
            (b"", b"", 0),  # git config credential.helper — no cred helper
            (b"", b"", 0),  # ls-remote empty
            (b"", b"rejected", 1),  # push fails
        ]
        await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": "feat", "project_id": "p"})
        self.mock_post_msg.assert_awaited_once()
        call_kwargs = self.mock_post_msg.await_args.kwargs
        assert call_kwargs["type"] == "status"
        assert "Auto-push failed" in call_kwargs["title"]

    async def test_no_pat_returns_false(self):
        from switchboard.git.operations import _ensure_branch_pushed
        self.mock_resolve_url.side_effect = ValueError("No PAT configured")
        result = await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": "feat", "project_id": "p"})
        assert result is False
        self.mock_run.assert_not_awaited()

    async def test_cred_helper_present_pushes_to_origin(self):
        from switchboard.git.operations import _ensure_branch_pushed
        # credential helper returns a helper path (non-empty)
        self.mock_run.side_effect = [
            (b"/path/to/helper.sh\n", b"", 0),  # git config credential.helper — has cred helper
            (b"", b"", 0),  # ls-remote empty (no remote branch)
            (b"", b"", 0),  # push succeeds
        ]
        await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": "feat", "project_id": "p"})
        push_call = self.mock_run.await_args_list[2]
        assert "push" in push_call.args
        assert "origin" in push_call.args
        assert "--force" in push_call.args
        # Must NOT use the raw URL
        assert "https://oauth2:ghp_test@github.com" not in str(push_call.args)

    async def test_cred_helper_absent_pushes_to_raw_url(self):
        from switchboard.git.operations import _ensure_branch_pushed
        # credential helper returns empty (not configured)
        self.mock_run.side_effect = [
            (b"", b"", 0),  # git config credential.helper — no cred helper
            (b"", b"", 0),  # ls-remote empty (no remote branch)
            (b"", b"", 0),  # push succeeds
        ]
        await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": "feat", "project_id": "p"})
        push_call = self.mock_run.await_args_list[2]
        assert "push" in push_call.args
        assert "https://oauth2:ghp_test@github.com/acme/widgets.git" in push_call.args
        assert "origin" not in push_call.args


# ---------------------------------------------------------------------------
# Push failure blocks gate pipeline
# ---------------------------------------------------------------------------

class TestPushFailureBlocksGatePipeline:
    """When _ensure_branch_pushed returns False, gate pipeline must be skipped."""

    @pytest.fixture(autouse=True)
    def _patches(self, tmp_path):
        import pwd
        from pathlib import Path

        self.log_dir = tmp_path / "logs"
        self.log_dir.mkdir()
        self.worktree = str(tmp_path / "wt")
        os.makedirs(self.worktree, exist_ok=True)

        mock_pw = MagicMock()
        mock_pw.pw_dir = str(tmp_path)

        self.mock_run_test_gate = AsyncMock()
        self.mock_dispatch_review = AsyncMock()
        self.mock_check_dependents = AsyncMock()
        self.mock_update_usage = AsyncMock()

        # Completing mock SDK client
        from claude_agent_sdk import ResultMessage as _RM
        result_msg = MagicMock(spec=_RM)
        result_msg.is_error = False
        result_msg.result = "Done."
        result_msg.stop_reason = "end_turn"
        result_msg.num_turns = 1
        result_msg.total_cost_usd = 0.001
        result_msg.duration_ms = 5000
        result_msg.duration_api_ms = 4800
        result_msg.session_id = None
        result_msg.usage = {
            "input_tokens": 10, "output_tokens": 5,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        }

        async def _fast_gen():
            yield result_msg

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.query = AsyncMock()
        mock_client.receive_response = MagicMock(return_value=_fast_gen())

        patches = [
            patch("switchboard.dispatch.sdk_session.ClaudeSDKClient", return_value=mock_client),
            patch("switchboard.git.operations._ensure_branch_pushed",
                  AsyncMock(return_value=False)),
            patch("switchboard.dispatch.gates._run_test_gate", self.mock_run_test_gate),
            patch("switchboard.dispatch.gates._dispatch_review", self.mock_dispatch_review),
            patch("switchboard.dispatch.engine._check_and_dispatch_dependents",
                  self.mock_check_dependents),
            patch("switchboard.dispatch.engine._update_usage", self.mock_update_usage),
            patch("switchboard.dispatch.sdk_session.pwd.getpwnam", return_value=mock_pw),
            patch("switchboard.notifications.slack.task_completed", AsyncMock()),
            patch("switchboard.notifications.slack.task_needs_review", AsyncMock()),
            patch("switchboard.dispatch.queue._drain_queue", AsyncMock()),
        ]
        for p in patches:
            p.start()
        self._patches = patches
        yield
        for p in patches:
            p.stop()

    async def test_push_fail_sets_push_failed_gate_status(self, db, sample_project, tmp_path):
        """When push fails after completion, gate_status must be 'push-failed'."""
        from switchboard.dispatch.sdk_session import _run_sdk_session

        task = await db.create_task(
            id="test-project/push-fail-gate",
            project_id="test-project",
            goal="Test push failure blocks gate",
            auto_test=False,
            auto_review=False,
        )
        await db.update_task(task["id"], status="working",
                             worktree_path=self.worktree)

        await _run_sdk_session(
            task_id="test-project/push-fail-gate",
            prompt="do the thing",
            worktree_path=self.worktree,
            session_id=None,
            is_resume=False,
            max_turns=10,
            max_wall_clock_minutes=30,
            log_dir=self.log_dir,
        )

        updated = await db.get_task("test-project/push-fail-gate")
        assert updated["gate_status"] == "push-failed", (
            f"Expected gate_status='push-failed', got {updated['gate_status']!r}"
        )

    async def test_push_fail_does_not_run_test_gate(self, db, sample_project, tmp_path):
        """When push fails, _run_test_gate must NOT be called."""
        from switchboard.dispatch.sdk_session import _run_sdk_session

        task = await db.create_task(
            id="test-project/push-fail-no-gate",
            project_id="test-project",
            goal="Test push failure blocks gate",
            auto_test=True,
            auto_review=True,
        )
        await db.update_task(task["id"], status="working",
                             worktree_path=self.worktree)

        await _run_sdk_session(
            task_id="test-project/push-fail-no-gate",
            prompt="do the thing",
            worktree_path=self.worktree,
            session_id=None,
            is_resume=False,
            max_turns=10,
            max_wall_clock_minutes=30,
            log_dir=self.log_dir,
        )

        self.mock_run_test_gate.assert_not_called()
        self.mock_dispatch_review.assert_not_called()
        self.mock_check_dependents.assert_not_called()

    async def test_session_id_captured_from_system_init(self, db, sample_project, tmp_path):
        """session_id should be captured early from SystemMessage(subtype='init')."""
        from switchboard.dispatch.sdk_session import _run_sdk_session
        from claude_agent_sdk import SystemMessage as _SM, ResultMessage as _RM

        task = await db.create_task(
            id="test-project/early-session",
            project_id="test-project",
            goal="Test early session_id capture",
            auto_test=False,
            auto_review=False,
        )
        await db.update_task(task["id"], status="working",
                             worktree_path=self.worktree)

        # Create a SystemMessage with subtype="init" carrying session_id
        init_msg = MagicMock(spec=_SM)
        init_msg.subtype = "init"
        init_msg.data = {"session_id": "early-sess-789"}

        result_msg = MagicMock(spec=_RM)
        result_msg.is_error = False
        result_msg.result = "Done."
        result_msg.stop_reason = "end_turn"
        result_msg.num_turns = 1
        result_msg.total_cost_usd = 0.001
        result_msg.duration_ms = 5000
        result_msg.duration_api_ms = 4800
        result_msg.session_id = None  # ResultMessage has no session_id
        result_msg.usage = {
            "input_tokens": 10, "output_tokens": 5,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        }

        async def _gen_with_init():
            yield init_msg
            yield result_msg

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.query = AsyncMock()
        mock_client.receive_response = MagicMock(return_value=_gen_with_init())

        with patch("switchboard.dispatch.sdk_session.ClaudeSDKClient", return_value=mock_client):
            await _run_sdk_session(
                task_id="test-project/early-session",
                prompt="do the thing",
                worktree_path=self.worktree,
                session_id=None,
                is_resume=False,
                max_turns=10,
                max_wall_clock_minutes=30,
                log_dir=self.log_dir,
            )

        updated = await db.get_task("test-project/early-session")
        assert updated["session_id"] == "early-sess-789", (
            f"Expected session_id='early-sess-789', got {updated['session_id']!r}"
        )


# ---------------------------------------------------------------------------
# _build_task_prompt — prompt construction
# ---------------------------------------------------------------------------

class TestBuildTaskPrompt:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_get_task = AsyncMock(return_value=None)
        self.mock_read_msgs = AsyncMock(return_value={"messages": []})

        self.mock_list_files = AsyncMock(return_value=[])

        patches = [
            patch("switchboard.db.get_task", self.mock_get_task),
            patch("switchboard.db.read_task_messages", self.mock_read_msgs),
            patch("switchboard.db.list_files", self.mock_list_files),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    def _make_project(self, **overrides):
        p = {"id": "test-proj", "repo": "git@github.com:x/y.git",
             "test_command": "pytest"}
        p.update(overrides)
        return p

    def _make_task(self, **overrides):
        t = {"id": "t1", "goal": "test", "branch": "feat-t1",
             "auto_test": False, "depends_on": None}
        t.update(overrides)
        return t

    async def test_includes_push_instruction(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(), self._make_task(), "do the thing")
        assert "push your branch" in result.lower()

    async def test_auto_test_tells_cc_not_to_run_tests(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(), self._make_task(auto_test=True), "do the thing")
        assert "automatically" in result.lower()

    async def test_no_auto_test_includes_test_command(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(test_command="php artisan test"),
            self._make_task(), "do the thing")
        assert "php artisan test" in result

    async def test_dependency_context_included(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        parent = {
            "id": "parent-task", "branch": "feat-parent",
            "goal": "build models", "status": "completed",
        }
        self.mock_get_task.return_value = parent
        self.mock_read_msgs.return_value = {
            "messages": [{"type": "result", "content": "Models done", "author": "cc-worker"}]
        }
        result = await _build_task_prompt(
            self._make_project(),
            self._make_task(depends_on="parent-task"),
            "build the API")
        assert "parent" in result.lower() or "feat-parent" in result

    async def test_identity_section_present(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(),
            self._make_task(dispatched_by="stephen", worktree_path="/work/t1"),
            "do the thing")
        assert "Ouvrage worker" in result
        assert "stephen" in result
        assert "/work/t1" in result

    async def test_identity_section_defaults_for_missing_fields(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        # dispatched_by and worktree_path absent — should not crash
        result = await _build_task_prompt(
            self._make_project(), self._make_task(), "do the thing")
        assert "Ouvrage worker" in result
        assert "system" in result  # default dispatched_by fallback

    async def test_component_context_included(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        mock_get_component = AsyncMock(return_value={
            "id": "comp-1", "name": "Auth Module",
            "description": "Handles login flows", "phase": "implementing",
        })
        mock_list_punchlist = AsyncMock(return_value=[])
        with patch("switchboard.db.get_component", mock_get_component), \
             patch("switchboard.db.list_punchlist", mock_list_punchlist):
            result = await _build_task_prompt(
                self._make_project(),
                self._make_task(component_id="comp-1"),
                "do the thing")
        assert "Auth Module" in result
        assert "Handles login flows" in result
        assert "implementing" in result

    async def test_component_punchlist_included(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        mock_get_component = AsyncMock(return_value={
            "id": "comp-1", "name": "Auth Module",
            "description": None, "phase": None,
        })
        mock_list_punchlist = AsyncMock(return_value=[
            {"id": 42, "item": "Fix logout bug", "status": "open"},
            {"id": 43, "item": "Token refresh", "status": "claimed"},
        ])
        with patch("switchboard.db.get_component", mock_get_component), \
             patch("switchboard.db.list_punchlist", mock_list_punchlist):
            result = await _build_task_prompt(
                self._make_project(),
                self._make_task(component_id="comp-1"),
                "do the thing")
        assert "Fix logout bug" in result
        assert "Token refresh" in result
        # Header should say "Punchlist items" (not "Open punchlist items")
        assert "Punchlist items for this component" in result
        assert "Open punchlist items" not in result

    async def test_no_component_context_when_no_component_id(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        mock_get_component = AsyncMock()
        with patch("switchboard.db.get_component", mock_get_component):
            result = await _build_task_prompt(
                self._make_project(), self._make_task(), "do the thing")
        mock_get_component.assert_not_called()
        assert "Component Context" not in result

    async def test_tool_inventory_present(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(), self._make_task(), "do the thing")
        assert "update_task_checklist" in result
        assert "post_task_message" in result
        assert "search(" in result
        assert "Context Discovery" in result
        assert "Progress Reporting" in result

    async def test_pipeline_awareness_present(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(), self._make_task(), "do the thing")
        assert "Gate Pipeline" in result or "gate" in result.lower()
        assert "review gate" in result.lower() or "Review gate" in result

    async def test_escalation_protocol_present(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(), self._make_task(), "do the thing")
        assert "Escalation Protocol" in result
        assert "Ambiguous spec" in result

    async def test_custom_escalation_criteria_injected(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(), self._make_task(), "do the thing",
            escalation_criteria="Always post question if touching prod DB.")
        assert "Always post question if touching prod DB." in result

    async def test_prohibitions_section_present(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(), self._make_task(), "do the thing")
        assert "What NOT To Do" in result
        assert "git config" in result
        assert "committing secrets" in result.lower() or "secrets" in result

    async def test_prohibitions_no_full_suite_when_auto_test_disabled(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(), self._make_task(auto_test=False), "do the thing")
        assert "full test suite" not in result

    async def test_prohibitions_includes_no_full_suite_when_auto_test_enabled(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(), self._make_task(auto_test=True), "do the thing")
        assert "full test suite" in result or "full suite" in result


# ---------------------------------------------------------------------------
# _build_resume_prompt — resume prompt construction
# ---------------------------------------------------------------------------

class TestBuildResumePrompt:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_get_task = AsyncMock(return_value=None)
        self.mock_get_checklist = AsyncMock(return_value=[])

        patches = [
            patch("switchboard.db.get_task", self.mock_get_task),
            patch("switchboard.db.get_checklist", self.mock_get_checklist),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    def _make_task(self, **overrides):
        t = {"id": "t1", "goal": "build the API", "branch": "feat-t1"}
        t.update(overrides)
        return t

    async def test_task_exists_includes_id_branch_goal(self):
        from switchboard.dispatch.sdk_session import _build_resume_prompt
        self.mock_get_task.return_value = self._make_task(
            id="task-xyz", branch="feat-xyz", goal="Refactor auth module")
        result = await _build_resume_prompt("task-xyz")
        assert "task-xyz" in result
        assert "feat-xyz" in result
        assert "Refactor auth module" in result

    async def test_task_not_found_returns_fallback_with_task_id(self):
        from switchboard.dispatch.sdk_session import _build_resume_prompt
        self.mock_get_task.return_value = None
        result = await _build_resume_prompt("missing-task")
        assert "missing-task" in result
        # Should be a short fallback, not crash
        assert len(result) > 0

    async def test_no_checklist_omits_checklist_section(self):
        from switchboard.dispatch.sdk_session import _build_resume_prompt
        self.mock_get_task.return_value = self._make_task()
        self.mock_get_checklist.return_value = []
        result = await _build_resume_prompt("t1")
        assert "Checklist" not in result

    async def test_checklist_renders_done_and_undone(self):
        from switchboard.dispatch.sdk_session import _build_resume_prompt
        self.mock_get_task.return_value = self._make_task()
        self.mock_get_checklist.return_value = [
            {"id": 1, "item": "Write tests", "done": True},
            {"id": 2, "item": "Update docs", "done": False},
        ]
        result = await _build_resume_prompt("t1")
        assert "✅" in result
        assert "⬜" in result
        assert "Write tests" in result
        assert "Update docs" in result

    async def test_includes_read_task_messages_instruction(self):
        from switchboard.dispatch.sdk_session import _build_resume_prompt
        self.mock_get_task.return_value = self._make_task(id="task-abc")
        result = await _build_resume_prompt("task-abc")
        assert "read_task_messages" in result


# ---------------------------------------------------------------------------
# _rebase_and_redispatch — rebase logic (mocked git)
# ---------------------------------------------------------------------------

class TestRebaseAndRedispatch:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_run = AsyncMock()
        self.mock_update_task = AsyncMock()
        self.mock_post_msg = AsyncMock()
        self.mock_lifecycle_execute = AsyncMock()

        patches = [
            patch("switchboard.git.operations._run_as_worker", self.mock_run),
            patch("switchboard.db.update_task", self.mock_update_task),
            patch("switchboard.db.post_task_message", self.mock_post_msg),
            patch("switchboard.dispatch.lifecycle.lifecycle.execute", self.mock_lifecycle_execute),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_successful_rebase(self):
        from switchboard.dispatch.engine import _rebase_and_redispatch
        dep = {
            "id": "task-b", "project_id": "proj", "goal": "do B",
            "worktree_path": "/work/proj/task-b", "branch": "feat-b",
        }
        parent = {"id": "task-a", "branch": "feat-a"}

        self.mock_run.side_effect = [
            (b"", b"", 0),  # git fetch
            (b"", b"", 0),  # git rebase success
        ]
        await _rebase_and_redispatch(dep, parent)

        # Gate state should be reset
        self.mock_update_task.assert_awaited()
        reset_call = self.mock_update_task.await_args_list[0]
        assert reset_call.kwargs["gate_status"] is None
        assert reset_call.kwargs["gate_retries"] == 0

        # Should dispatch via lifecycle
        self.mock_lifecycle_execute.assert_awaited_once()
        call_args = self.mock_lifecycle_execute.await_args[0]
        assert call_args[0] == "task-b"
        assert call_args[1] == "dispatch"

    async def test_rebase_conflict_aborts_and_dispatches(self):
        from switchboard.dispatch.engine import _rebase_and_redispatch
        dep = {
            "id": "task-b", "project_id": "proj", "goal": "do B",
            "worktree_path": "/work/proj/task-b", "branch": "feat-b",
        }
        parent = {"id": "task-a", "branch": "feat-a"}

        self.mock_run.side_effect = [
            (b"", b"", 0),  # git fetch
            (b"", b"CONFLICT", 1),  # git rebase fails
            (b"", b"", 0),  # git rebase --abort
        ]
        await _rebase_and_redispatch(dep, parent)

        # Should have called rebase --abort
        abort_call = self.mock_run.await_args_list[2]
        assert "--abort" in abort_call.args

        # Should still dispatch via lifecycle (CC handles conflicts)
        self.mock_lifecycle_execute.assert_awaited_once()
        call_args = self.mock_lifecycle_execute.await_args[0]
        assert call_args[0] == "task-b"
        assert call_args[1] == "dispatch"


# _is_binary — binary file detection
# ---------------------------------------------------------------------------

class TestIsBinary:
    def setup_method(self):
        from switchboard.git.files import _is_binary
        self.fn = _is_binary

    def test_text_is_not_binary(self):
        assert self.fn(b"hello world\nsome text\n") is False

    def test_null_byte_is_binary(self):
        assert self.fn(b"some\x00data") is True

    def test_empty_is_not_binary(self):
        assert self.fn(b"") is False

    def test_null_beyond_8kb_ignored(self):
        # Null byte after 8192 bytes should NOT trigger detection
        data = b"a" * 8192 + b"\x00"
        assert self.fn(data) is False

    def test_null_at_8kb_boundary(self):
        # Null byte at exactly position 8191 (within 8KB) triggers detection
        data = b"a" * 8191 + b"\x00"
        assert self.fn(data) is True


# ---------------------------------------------------------------------------
# _validate_path — path traversal prevention
# ---------------------------------------------------------------------------

class TestValidatePath:
    def setup_method(self):
        from switchboard.git.files import _validate_path
        self.fn = _validate_path

    def test_normal_path_ok(self):
        assert self.fn("src/server.py") is None

    def test_root_ok(self):
        assert self.fn("") is None

    def test_dotdot_rejected(self):
        assert self.fn("../etc/passwd") is not None

    def test_dotdot_in_middle_rejected(self):
        assert self.fn("src/../etc/passwd") is not None

    def test_single_dot_ok(self):
        # "." is a valid path component, not ".."
        assert self.fn("./src") is None

    def test_filename_with_dots_ok(self):
        assert self.fn("src/server.py") is None


# ---------------------------------------------------------------------------
# _handle_list_task_files — git ls-tree integration
# ---------------------------------------------------------------------------

class TestListTaskFiles:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        import switchboard.git.files as _files_mod
        # Clear module-level fetch cache so tests don't interfere with each other
        _files_mod._fetch_cache.clear()

        self.mock_get_task = AsyncMock()
        self.mock_get_project = AsyncMock()
        self.mock_git_run = AsyncMock()
        self.mock_isdir = patch("os.path.isdir").start()

        patches = [
            patch("switchboard.git.files.db.get_task", self.mock_get_task),
            patch("switchboard.git.files.db.get_project", self.mock_get_project),
            patch("switchboard.git.files._git_run", self.mock_git_run),
        ]
        for p in patches:
            p.start()
        yield
        patch.stopall()

    def _make_task(self, worktree_path=None, branch="feat/my-feature", status="working"):
        return {
            "id": "proj/my-task",
            "project_id": "proj",
            "worktree_path": worktree_path,
            "branch": branch,
            "status": status,
        }

    def _make_project(self):
        return {"id": "proj", "working_dir": "/work/proj"}

    async def test_active_worktree_uses_head(self):
        from switchboard.git.files import _handle_list_task_files
        self.mock_get_task.return_value = self._make_task(worktree_path="/work/proj/my-task")
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = True
        self.mock_git_run.return_value = (b"README.md\nserver.py\n", 0)

        result = await _handle_list_task_files({"task_id": "proj/my-task"})

        assert result["files"] == ["README.md", "server.py"]
        assert result["ref_used"] == "HEAD"
        assert "git_dir" not in result

    async def test_released_task_uses_origin_branch(self):
        from switchboard.git.files import _handle_list_task_files
        self.mock_get_task.return_value = self._make_task(worktree_path=None, branch="feat/released", status="completed")
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = False

        # fetch returns ok, rev-parse returns ok, ls-tree returns ok
        self.mock_git_run.side_effect = [
            (b"", 0),   # git fetch origin --prune
            (b"abc123\n", 0),  # git rev-parse --verify origin/feat/released
            (b"README.md\n", 0),  # git ls-tree
        ]

        result = await _handle_list_task_files({"task_id": "proj/my-task"})

        assert result["ref_used"] == "origin/feat/released"
        assert result["files"] == ["README.md"]

    async def test_inaccessible_task_returns_error(self):
        from switchboard.git.files import _handle_list_task_files
        self.mock_get_task.return_value = self._make_task(worktree_path=None, branch=None, status="cancelled")
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = False

        result = await _handle_list_task_files({"task_id": "proj/my-task"})

        assert "error" in result
        assert "not accessible" in result["error"]

    async def test_task_not_found(self):
        from switchboard.git.files import _handle_list_task_files
        self.mock_get_task.return_value = None

        result = await _handle_list_task_files({"task_id": "proj/nonexistent"})

        assert "error" in result
        assert "not found" in result["error"]

    async def test_path_traversal_rejected(self):
        from switchboard.git.files import _handle_list_task_files
        self.mock_get_task.return_value = self._make_task(worktree_path="/work/proj/my-task")
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = True

        result = await _handle_list_task_files({"task_id": "proj/my-task", "path": "../etc"})

        assert "error" in result
        assert ".." in result["error"]

    async def test_recursive_flag_passed(self):
        from switchboard.git.files import _handle_list_task_files
        self.mock_get_task.return_value = self._make_task(worktree_path="/work/proj/my-task")
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = True
        self.mock_git_run.return_value = (b"src/a.py\nsrc/b.py\n", 0)

        result = await _handle_list_task_files({
            "task_id": "proj/my-task",
            "recursive": True,
        })

        assert result["recursive"] is True
        # Verify -r was in the git command
        call_args = self.mock_git_run.call_args
        assert "-r" in call_args[0][0]


# ---------------------------------------------------------------------------
# _handle_get_task_file — git show integration
# ---------------------------------------------------------------------------

class TestGetTaskFile:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        import switchboard.git.files as _files_mod
        _files_mod._fetch_cache.clear()

        self.mock_get_task = AsyncMock()
        self.mock_get_project = AsyncMock()
        self.mock_git_run = AsyncMock()
        self.mock_isdir = patch("os.path.isdir").start()

        patches = [
            patch("switchboard.git.files.db.get_task", self.mock_get_task),
            patch("switchboard.git.files.db.get_project", self.mock_get_project),
            patch("switchboard.git.files._git_run", self.mock_git_run),
        ]
        for p in patches:
            p.start()
        yield
        patch.stopall()

    def _make_task(self, worktree_path="/work/proj/my-task", branch="feat/x"):
        return {
            "id": "proj/my-task",
            "project_id": "proj",
            "worktree_path": worktree_path,
            "branch": branch,
            "status": "working",
        }

    def _make_project(self):
        return {"id": "proj", "working_dir": "/work/proj"}

    async def test_returns_text_content(self):
        from switchboard.git.files import _handle_get_task_file
        self.mock_get_task.return_value = self._make_task()
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = True
        self.mock_git_run.side_effect = [
            (b"blob\n", 0),  # cat-file -t
            (b"def hello():\n    return 'world'\n", 0),  # git show
        ]

        result = await _handle_get_task_file({"task_id": "proj/my-task", "path": "src/hello.py"})

        assert result["content"] == "def hello():\n    return 'world'\n"
        assert result["binary"] is False
        assert result["truncated"] is False
        assert "git_dir" not in result

    async def test_binary_file_refused(self):
        from switchboard.git.files import _handle_get_task_file
        self.mock_get_task.return_value = self._make_task()
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = True
        self.mock_git_run.side_effect = [
            (b"blob\n", 0),  # cat-file -t
            (b"PNG\x00binary\x00data", 0),  # git show
        ]

        result = await _handle_get_task_file({"task_id": "proj/my-task", "path": "logo.png"})

        assert "error" in result
        assert result["binary"] is True

    async def test_large_file_truncated(self):
        from switchboard.git.files import _handle_get_task_file
        self.mock_get_task.return_value = self._make_task()
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = True
        content = b"x" * 2000
        self.mock_git_run.side_effect = [
            (b"blob\n", 0),  # cat-file -t
            (content, 0),  # git show
        ]

        result = await _handle_get_task_file({
            "task_id": "proj/my-task",
            "path": "big.txt",
            "max_bytes": 100,
        })

        assert result["truncated"] is True
        assert len(result["content"]) == 100
        assert result["size"] == 2000

    async def test_file_not_found(self):
        from switchboard.git.files import _handle_get_task_file
        self.mock_get_task.return_value = self._make_task()
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = True
        self.mock_git_run.return_value = (b"", 128)

        result = await _handle_get_task_file({"task_id": "proj/my-task", "path": "nonexistent.py"})

        assert "error" in result
        assert "not found" in result["error"]

    async def test_path_traversal_rejected(self):
        from switchboard.git.files import _handle_get_task_file
        self.mock_get_task.return_value = self._make_task()
        self.mock_get_project.return_value = self._make_project()

        result = await _handle_get_task_file({
            "task_id": "proj/my-task",
            "path": "../../etc/shadow",
        })

        assert "error" in result
        assert ".." in result["error"]

    async def test_inaccessible_task(self):
        from switchboard.git.files import _handle_get_task_file
        self.mock_get_task.return_value = {
            "id": "proj/my-task", "project_id": "proj",
            "worktree_path": None, "branch": None, "status": "cancelled",
        }
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = False

        result = await _handle_get_task_file({"task_id": "proj/my-task", "path": "foo.py"})

        assert "error" in result
        assert "not accessible" in result["error"]

    async def test_directory_path_returns_clear_error(self):
        from switchboard.git.files import _handle_get_task_file
        self.mock_get_task.return_value = self._make_task()
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = True
        # cat-file returns "tree" for a directory path
        self.mock_git_run.side_effect = [(b"tree\n", 0)]

        result = await _handle_get_task_file({"task_id": "proj/my-task", "path": "src"})

        assert "error" in result
        assert "directory" in result["error"].lower()
        assert "list_task_files" in result["error"]
        # git show should NOT have been called (only one mock call consumed)
        assert self.mock_git_run.call_count == 1

    async def test_git_dir_not_in_response(self):
        from switchboard.git.files import _handle_get_task_file
        self.mock_get_task.return_value = self._make_task()
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = True
        self.mock_git_run.side_effect = [
            (b"blob\n", 0),
            (b"content\n", 0),
        ]

        result = await _handle_get_task_file({"task_id": "proj/my-task", "path": "foo.py"})

        assert "git_dir" not in result


# ---------------------------------------------------------------------------
# _git_run — timeout behaviour
# ---------------------------------------------------------------------------

class TestGitRunTimeout:
    async def test_timeout_raises(self):
        import asyncio
        from unittest.mock import AsyncMock, patch, MagicMock
        from switchboard.git.files import _git_run

        # Simulate a process that hangs forever
        mock_proc = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(asyncio.TimeoutError):
                await _git_run(["status"], "/some/path", timeout=0.001)


# ---------------------------------------------------------------------------
# Fetch TTL cache — _resolve_git_ref skips fetch within TTL window
# ---------------------------------------------------------------------------

class TestFetchCache:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        import switchboard.git.files as _files_mod
        _files_mod._fetch_cache.clear()

        self.mock_get_task = AsyncMock()
        self.mock_get_project = AsyncMock()
        self.mock_git_run = AsyncMock()
        self.mock_isdir = patch("os.path.isdir").start()

        patches = [
            patch("switchboard.git.files.db.get_task", self.mock_get_task),
            patch("switchboard.git.files.db.get_project", self.mock_get_project),
            patch("switchboard.git.files._git_run", self.mock_git_run),
        ]
        for p in patches:
            p.start()
        yield
        patch.stopall()

    def _make_task(self):
        return {
            "id": "proj/my-task",
            "project_id": "proj",
            "worktree_path": None,
            "branch": "feat/released",
            "status": "completed",
        }

    def _make_project(self):
        return {"id": "proj", "working_dir": "/work/proj"}

    async def test_fetch_skipped_within_ttl(self):
        """Second call within TTL should not trigger another git fetch."""
        from switchboard.git.files import _handle_list_task_files
        import switchboard.git.files as _files_mod

        self.mock_get_task.return_value = self._make_task()
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = False

        # First call: fetch + rev-parse + ls-tree
        self.mock_git_run.side_effect = [
            (b"", 0),          # fetch
            (b"abc123\n", 0),  # rev-parse
            (b"README.md\n", 0),  # ls-tree
            # Second call: rev-parse + ls-tree (NO fetch)
            (b"abc123\n", 0),  # rev-parse
            (b"server.py\n", 0),  # ls-tree
        ]

        result1 = await _handle_list_task_files({"task_id": "proj/my-task"})
        result2 = await _handle_list_task_files({"task_id": "proj/my-task"})

        assert result1["files"] == ["README.md"]
        assert result2["files"] == ["server.py"]
        # Total calls: 3 (first) + 2 (second, skipped fetch) = 5
        assert self.mock_git_run.call_count == 5

    async def test_fetch_runs_when_ttl_expired(self):
        """Fetch should re-run after TTL expires."""
        import time
        from switchboard.git.files import _handle_list_task_files
        import switchboard.git.files as _files_mod

        self.mock_get_task.return_value = self._make_task()
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = False

        bare_path = "/work/proj/.bare"
        # Pre-seed cache with a stale timestamp
        _files_mod._fetch_cache[bare_path] = time.monotonic() - (_files_mod._FETCH_TTL + 1.0)

        # Should trigger a fresh fetch
        self.mock_git_run.side_effect = [
            (b"", 0),           # fetch (TTL expired)
            (b"abc123\n", 0),   # rev-parse
            (b"README.md\n", 0),  # ls-tree
        ]

        result = await _handle_list_task_files({"task_id": "proj/my-task"})

        assert result["files"] == ["README.md"]
        assert self.mock_git_run.call_count == 3


# ---------------------------------------------------------------------------
# _resolve_git_ref — direct unit tests for the resolution logic
# ---------------------------------------------------------------------------

class TestResolveGitRef:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        import switchboard.git.files as _files_mod
        _files_mod._fetch_cache.clear()

        self.mock_git_run = AsyncMock()
        self.mock_isdir = patch("os.path.isdir").start()
        patch("switchboard.git.files._git_run", self.mock_git_run).start()
        yield
        patch.stopall()

    def _make_task(self, worktree_path=None, branch="feat/my-feature"):
        return {"worktree_path": worktree_path, "branch": branch}

    def _make_project(self):
        return {"working_dir": "/work/proj"}

    async def test_active_worktree_returns_head(self):
        """Priority 1: active worktree on disk → (worktree_path, HEAD)."""
        from switchboard.git.files import _resolve_git_ref
        self.mock_isdir.return_value = True
        task = self._make_task(worktree_path="/work/proj/worktrees/my-task")

        result = await _resolve_git_ref(task, self._make_project())

        assert result == ("/work/proj/worktrees/my-task", "HEAD")
        self.mock_git_run.assert_not_called()

    async def test_branch_on_origin_returns_bare_ref(self):
        """Priority 2: no worktree, branch exists on origin → (bare_path, origin/branch)."""
        from switchboard.git.files import _resolve_git_ref
        self.mock_isdir.return_value = False
        # fetch succeeds, rev-parse finds branch
        self.mock_git_run.side_effect = [
            (b"", 0),          # git fetch origin --prune -q
            (b"abc123\n", 0),  # git rev-parse --verify origin/feat/my-feature
        ]

        result = await _resolve_git_ref(
            self._make_task(worktree_path=None, branch="feat/my-feature"),
            self._make_project(),
        )

        assert result == ("/work/proj/.bare", "origin/feat/my-feature")

    async def test_branch_not_on_origin_returns_none(self):
        """Priority 2 fails: branch not found on origin → None."""
        from switchboard.git.files import _resolve_git_ref
        self.mock_isdir.return_value = False
        self.mock_git_run.side_effect = [
            (b"", 0),   # fetch ok
            (b"", 128), # rev-parse fails — branch not found
        ]

        result = await _resolve_git_ref(
            self._make_task(worktree_path=None, branch="feat/gone"),
            self._make_project(),
        )

        assert result is None

    async def test_no_branch_returns_none(self):
        """No worktree and no branch → None without any git calls."""
        from switchboard.git.files import _resolve_git_ref
        self.mock_isdir.return_value = False

        result = await _resolve_git_ref(
            self._make_task(worktree_path=None, branch=None),
            self._make_project(),
        )

        assert result is None
        self.mock_git_run.assert_not_called()

    async def test_fetch_failure_does_not_poison_cache(self):
        """If fetch fails, the cache entry must NOT be set so the next call retries."""
        import time
        import switchboard.git.files as _files_mod
        from switchboard.git.files import _resolve_git_ref

        self.mock_isdir.return_value = False
        bare_path = "/work/proj/.bare"

        # First call: fetch fails, rev-parse also fails
        self.mock_git_run.side_effect = [
            (b"", 1),   # fetch fails
            (b"", 128), # rev-parse fails
            # Second call: fetch succeeds this time, rev-parse succeeds
            (b"", 0),
            (b"abc123\n", 0),
        ]

        task = self._make_task(worktree_path=None, branch="feat/my-feature")
        project = self._make_project()

        result1 = await _resolve_git_ref(task, project)
        # Cache should NOT have been set — fetch failed
        assert bare_path not in _files_mod._fetch_cache

        result2 = await _resolve_git_ref(task, project)
        assert result2 == (bare_path, "origin/feat/my-feature")
        # Cache should now be set after successful fetch
        assert bare_path in _files_mod._fetch_cache


# ---------------------------------------------------------------------------
# _handle_post — reactive conversation injection
# ---------------------------------------------------------------------------

class TestReactiveConversationInjection:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_post_message = AsyncMock(return_value={"id": 1, "conversation_id": "conv-a"})
        self.mock_get_working_tasks = AsyncMock(return_value=[])
        self.mock_post_task_message = AsyncMock(return_value={"id": 99})

        patches = [
            patch("switchboard.db.post_message", self.mock_post_message),
            patch("switchboard.db.get_working_tasks_for_conversation", self.mock_get_working_tasks),
            patch("switchboard.db.post_task_message", self.mock_post_task_message),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_injects_nudge_for_working_tasks(self):
        from switchboard.server.handlers.conversations import _handle_post
        self.mock_get_working_tasks.return_value = ["proj/task-1", "proj/task-2"]
        result = await _handle_post({
            "conversation_id": "conv-a",
            "author": "stephen",
            "content": "Here is a new finding",
        })
        assert result["id"] == 1
        assert self.mock_post_task_message.await_count == 2
        call_kwargs = self.mock_post_task_message.await_args_list[0].kwargs
        assert call_kwargs["task_id"] == "proj/task-1"
        assert call_kwargs["author"] == "switchboard"
        assert "conv-a" in call_kwargs["content"]
        assert "stephen" in call_kwargs["content"]

    async def test_skips_injection_for_cc_worker_author(self):
        from switchboard.server.handlers.conversations import _handle_post
        self.mock_get_working_tasks.return_value = ["proj/task-1"]
        await _handle_post({
            "conversation_id": "conv-a",
            "author": "cc-worker",
            "content": "Task completed successfully",
        })
        self.mock_get_working_tasks.assert_not_awaited()
        self.mock_post_task_message.assert_not_awaited()

    async def test_skips_injection_for_status_type(self):
        from switchboard.server.handlers.conversations import _handle_post
        self.mock_get_working_tasks.return_value = ["proj/task-1"]
        await _handle_post({
            "conversation_id": "conv-a",
            "author": "stephen",
            "type": "status",
            "content": "Still thinking...",
        })
        self.mock_get_working_tasks.assert_not_awaited()
        self.mock_post_task_message.assert_not_awaited()

    async def test_injection_failure_is_non_blocking(self):
        from switchboard.server.handlers.conversations import _handle_post
        self.mock_get_working_tasks.side_effect = Exception("DB is down")
        result = await _handle_post({
            "conversation_id": "conv-a",
            "author": "stephen",
            "content": "Some update",
        })
        assert result["id"] == 1
        self.mock_post_task_message.assert_not_awaited()

    async def test_no_working_tasks_no_injection(self):
        from switchboard.server.handlers.conversations import _handle_post
        self.mock_get_working_tasks.return_value = []
        await _handle_post({
            "conversation_id": "conv-a",
            "author": "stephen",
            "content": "Update with no active tasks",
        })
        self.mock_post_task_message.assert_not_awaited()

    async def test_preview_uses_title_when_present(self):
        from switchboard.server.handlers.conversations import _handle_post
        self.mock_get_working_tasks.return_value = ["proj/task-1"]
        await _handle_post({
            "conversation_id": "conv-a",
            "author": "stephen",
            "title": "Important finding",
            "content": "Long content that should not appear in preview",
        })
        call_kwargs = self.mock_post_task_message.await_args_list[0].kwargs
        assert "Important finding" in call_kwargs["content"]
        assert "Long content" not in call_kwargs["content"]


# ---------------------------------------------------------------------------
# Held default logic — dispatch_task handler applies sensible defaults
# ---------------------------------------------------------------------------

class TestHeldDefaults:
    """_handle_dispatch_task applies held defaults: standalone=True, chain=False."""

    @pytest.fixture(autouse=True)
    def mock_anthropic_key(self):
        """Bypass Anthropic key guard so these tests focus on held-default logic."""
        with patch("switchboard.server.handlers.tasks.db.get_user_credentials",
                   return_value={"anthropic_api_key": "sk-ant-test"}):
            with patch("switchboard.server.handlers.tasks.db.get_instance",
                       return_value={"owner_user_id": None}):
                yield

    async def test_standalone_task_defaults_to_held(self, db, sample_project):
        """Standalone task (no depends_on) defaults to held=true."""
        from switchboard.server.handlers.tasks import _handle_dispatch_task
        result = await _handle_dispatch_task({
            "project_id": "test-project",
            "id": "standalone-held",
            "goal": "Standalone task",
        })
        assert result.get("held") is True
        task = await db.get_task("test-project/standalone-held")
        assert task["held"]

    async def test_chain_task_defaults_to_held_false(self, db, sample_project):
        """Chain task (with depends_on) defaults to held=false — waiting on parent."""
        from switchboard.server.handlers.tasks import _handle_dispatch_task
        # Parent task not yet gate-passed — child will wait but NOT be held
        await db.create_task(
            id="test-project/parent-for-chain", project_id="test-project", goal="Parent",
        )
        result = await _handle_dispatch_task({
            "project_id": "test-project",
            "id": "chain-not-held",
            "goal": "Chain task",
            "depends_on": "parent-for-chain",
        })
        # Returns waiting_on (not held)
        assert result.get("status") == "ready"
        assert result.get("waiting_on") == "test-project/parent-for-chain"
        assert not result.get("held")
        task = await db.get_task("test-project/chain-not-held")
        assert not task["held"]

    async def test_explicit_held_false_overrides_standalone_default(self, db, sample_project, mock_git):
        """Explicit held=false overrides the standalone default."""
        from switchboard.server.handlers.tasks import _handle_dispatch_task
        with patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()):
            result = await _handle_dispatch_task({
                "project_id": "test-project",
                "id": "standalone-explicit-false",
                "goal": "Standalone but explicitly not held",
                "held": False,
            })
        assert not result.get("held")
        task = await db.get_task("test-project/standalone-explicit-false")
        assert not task["held"]

    async def test_explicit_held_true_overrides_chain_default(self, db, sample_project):
        """Explicit held=true overrides chain default of false."""
        from switchboard.server.handlers.tasks import _handle_dispatch_task
        parent = await db.create_task(
            id="test-project/parent-for-held-chain", project_id="test-project", goal="Parent",
        )
        result = await _handle_dispatch_task({
            "project_id": "test-project",
            "id": "chain-explicit-held",
            "goal": "Chain task held explicitly",
            "depends_on": "parent-for-held-chain",
            "held": True,
        })
        assert result.get("held") is True
        task = await db.get_task("test-project/chain-explicit-held")
        assert task["held"]


# ---------------------------------------------------------------------------
# Project create validation — required config fields
# ---------------------------------------------------------------------------

class TestProjectCreateValidation:
    """_handle_create_project rejects missing required config fields."""

    @pytest.fixture(autouse=True)
    def mock_pat_validation(self):
        """Bypass PAT guard so tests focus on config-field validation logic."""
        with patch("switchboard.server.handlers.projects._validate_github_pat_for_repo",
                   return_value=None):
            yield

    async def test_missing_all_required_fields_returns_error(self, db):
        from switchboard.server.handlers.projects import _handle_create_project
        result = await _handle_create_project({
            "id": "new-proj",
            "repo": "git@github.com:acme/new.git",
        })
        assert "error" in result
        assert "Missing required config fields" in result["error"]
        for field in ["model", "review_model", "auto_test", "auto_review", "auto_pr", "auto_merge", "max_turns", "max_wall_clock"]:
            assert field in result["error"]

    async def test_missing_single_field_returns_error(self, db):
        from switchboard.server.handlers.projects import _handle_create_project
        result = await _handle_create_project({
            "id": "new-proj2",
            "repo": "git@github.com:acme/new2.git",
            "model": "sonnet",
            "review_model": "opus",
            "auto_test": True,
            "auto_review": True,
            "auto_pr": False,
            "auto_merge": False,
            "max_turns": 200,
            # max_wall_clock missing
        })
        assert "error" in result
        assert "max_wall_clock" in result["error"]
        assert "model" not in result["error"]

    async def test_all_required_fields_present_proceeds(self, db):
        from switchboard.server.handlers.projects import _handle_create_project
        result = await _handle_create_project({
            "id": "valid-proj",
            "repo": "git@github.com:acme/valid.git",
            "folder_name": "valid-proj-test",
            "model": "sonnet",
            "review_model": "opus",
            "auto_test": True,
            "auto_review": True,
            "auto_pr": False,
            "auto_merge": False,
            "max_turns": 200,
            "max_wall_clock": 30,
        })
        # No error — proceeds to db.create_project
        assert "error" not in result
        assert result["id"] == "valid-proj"


