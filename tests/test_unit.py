"""Tier 1: Unit tests — pure logic, no real DB/git/CC.

Tests core functions by mocking database and subprocess calls.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# _tail_lines — pure function, no mocks needed
# ---------------------------------------------------------------------------

class TestTailLines:
    def setup_method(self):
        from tasks import _tail_lines
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
        from tasks import _is_pid_alive
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
            patch("tasks.db.get_dependents", self.mock_get_dependents),
            patch("tasks.db.update_task", self.mock_update_task),
            patch("tasks.cancel_task", self.mock_cancel_task),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_no_dependents(self):
        from tasks import _invalidate_chain
        self.mock_get_dependents.return_value = []
        await _invalidate_chain("task-a")
        self.mock_get_dependents.assert_awaited_once_with("task-a")
        self.mock_update_task.assert_not_awaited()

    async def test_cancels_working_tasks(self):
        from tasks import _invalidate_chain
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
        from tasks import _invalidate_chain
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
        from tasks import _invalidate_chain
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
        from tasks import _invalidate_chain
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
        self.mock_retry = AsyncMock()
        self.mock_notify = AsyncMock()

        patches = [
            patch("tasks.db.read_task_messages", self.mock_read_msgs),
            patch("tasks.db.update_task", self.mock_update_task),
            patch("tasks.db.get_task", self.mock_get_task),
            patch("tasks._check_and_dispatch_dependents", self.mock_check_deps),
            patch("tasks.retry_task", self.mock_retry),
            patch("tasks.notify.task_needs_review", self.mock_notify),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_approved_passes_gate(self):
        from tasks import _process_review_result_inline
        self.mock_read_msgs.return_value = {
            "messages": [
                {"type": "review", "title": "APPROVED", "content": "Looks good"},
            ]
        }
        await _process_review_result_inline("task-1")
        # Should update gate to passed
        assert any(
            call.kwargs.get("gate_status") == "passed"
            for call in self.mock_update_task.await_args_list
        )
        self.mock_check_deps.assert_awaited_once_with("task-1")

    async def test_rejected_retries_if_under_limit(self):
        from tasks import _process_review_result_inline
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
        self.mock_retry.assert_awaited_once()

    async def test_rejected_escalates_after_max_retries(self):
        from tasks import _process_review_result_inline
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
        self.mock_retry.assert_not_awaited()
        # Should mark needs-review
        assert any(
            call.kwargs.get("status") == "needs-review"
            for call in self.mock_update_task.await_args_list
        )

    async def test_no_review_message_goes_to_rejection_path(self):
        """No review message = falls to else branch (rejection)."""
        from tasks import _process_review_result_inline
        self.mock_read_msgs.return_value = {"messages": []}
        self.mock_get_task.return_value = {
            "id": "task-1",
            "goal": "test",
            "gate_retries": 0,
            "max_gate_retries": 3,
        }
        await _process_review_result_inline("task-1")
        # With no review message, review_msg is None, condition fails → rejection path
        self.mock_retry.assert_awaited_once()


# ---------------------------------------------------------------------------
# _check_and_dispatch_dependents — routing logic
# ---------------------------------------------------------------------------

class TestCheckAndDispatchDependents:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_get_task = AsyncMock()
        self.mock_get_dependents = AsyncMock()
        self.mock_dispatch = AsyncMock()
        self.mock_rebase = AsyncMock()
        self.mock_pr = AsyncMock()
        self.mock_drain = AsyncMock()
        self.mock_auto_merge = AsyncMock(return_value=True)
        self.mock_auto_release = AsyncMock()
        self.mock_resolve_punchlist = AsyncMock(return_value=0)
        self.mock_post_msg = AsyncMock()

        patches = [
            patch("tasks.db.get_task", self.mock_get_task),
            patch("tasks.db.get_dependents", self.mock_get_dependents),
            patch("tasks.dispatch_task", self.mock_dispatch),
            patch("tasks._rebase_and_redispatch", self.mock_rebase),
            patch("tasks._maybe_create_pr", self.mock_pr),
            patch("tasks._drain_queue", self.mock_drain),
            patch("tasks._perform_auto_merge", self.mock_auto_merge),
            patch("tasks._auto_release_worktree", self.mock_auto_release),
            patch("tasks.db.resolve_punchlist_items_for_task", self.mock_resolve_punchlist),
            patch("tasks.db.post_task_message", self.mock_post_msg),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_dispatches_ready_dependents(self):
        from tasks import _check_and_dispatch_dependents
        self.mock_get_task.return_value = {
            "id": "task-a", "project_id": "proj", "gate_passed_at": "2026-01-01",
            "auto_test": True,
        }
        self.mock_get_dependents.return_value = [
            {"id": "task-b", "status": "ready", "gate_status": None,
             "project_id": "proj", "goal": "do B"},
        ]
        await _check_and_dispatch_dependents("task-a")
        self.mock_dispatch.assert_awaited_once()
        call_kwargs = self.mock_dispatch.await_args.kwargs
        assert call_kwargs["task_id"] == "task-b"

    async def test_rebases_stale_completed(self):
        from tasks import _check_and_dispatch_dependents
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
        from tasks import _check_and_dispatch_dependents
        self.mock_get_task.return_value = {
            "id": "task-a", "gate_passed_at": None,
        }
        await _check_and_dispatch_dependents("task-a")
        self.mock_get_dependents.assert_not_awaited()

    async def test_creates_pr_when_no_dependents(self):
        from tasks import _check_and_dispatch_dependents
        self.mock_get_task.return_value = {
            "id": "task-a", "project_id": "proj", "gate_passed_at": "2026-01-01",
        }
        self.mock_get_dependents.return_value = []
        await _check_and_dispatch_dependents("task-a")
        self.mock_pr.assert_awaited_once_with("task-a")

    async def test_held_task_skips_dispatch(self):
        """Fix 1 regression: held tasks must NOT be dispatched."""
        from tasks import _check_and_dispatch_dependents
        self.mock_get_task.return_value = {
            "id": "task-a", "project_id": "proj", "gate_passed_at": "2026-01-01",
        }
        self.mock_get_dependents.return_value = [
            {"id": "task-b", "status": "ready", "held": True,
             "project_id": "proj", "goal": "do B"},
        ]
        await _check_and_dispatch_dependents("task-a")
        self.mock_dispatch.assert_not_awaited()

    async def test_non_held_ready_task_actually_dispatches(self):
        """Fix 1: non-held ready dependent task must actually call dispatch_task."""
        from tasks import _check_and_dispatch_dependents
        self.mock_get_task.return_value = {
            "id": "task-a", "project_id": "proj", "gate_passed_at": "2026-01-01",
        }
        self.mock_get_dependents.return_value = [
            {"id": "task-b", "status": "ready", "held": False,
             "project_id": "proj", "goal": "do B", "auto_test": True},
        ]
        await _check_and_dispatch_dependents("task-a")
        self.mock_dispatch.assert_awaited_once()
        assert self.mock_dispatch.await_args.kwargs["task_id"] == "task-b"

    async def test_mixed_held_and_non_held(self):
        """Fix 1: only the non-held ready task dispatches; held one is skipped."""
        from tasks import _check_and_dispatch_dependents
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
        self.mock_dispatch.assert_awaited_once()
        assert self.mock_dispatch.await_args.kwargs["task_id"] == "task-b"


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
            patch("tasks.db.list_tasks", self.mock_list_tasks),
            patch("tasks.db.get_task", self.mock_get_task),
            patch("tasks.db.update_task", self.mock_update_task),
            patch("tasks.db.post_task_message", self.mock_post_msg),
            patch("tasks.db.read_task_messages", self.mock_read_msgs),
            patch("tasks.db.get_project", AsyncMock(return_value=None)),
            patch("tasks.db.get_component", AsyncMock(return_value=None)),
            patch("tasks._recover_single_task", self.mock_recover),
            patch("tasks.notify", self.mock_notify),
            patch("tasks.asyncio.sleep", self.mock_sleep),
            patch("tasks.retry_task", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    def _make_task(self, task_id, idle_seconds, has_active_client):
        from datetime import datetime, timezone, timedelta
        last = (datetime.now(timezone.utc) - timedelta(seconds=idle_seconds)).isoformat()
        import tasks
        if has_active_client:
            tasks._active_clients[task_id] = object()
        return {"id": task_id, "status": "working", "last_activity": last}

    def teardown_method(self):
        import tasks
        tasks._active_clients.clear()

    async def test_stall_warning_fires_for_active_client_task(self):
        """Fix 3: stall warning posts when active-client task is idle >=300s."""
        import tasks
        task = self._make_task("proj/stalled-1", idle_seconds=310, has_active_client=True)
        self.mock_list_tasks.side_effect = lambda status=None: (
            [task] if status == "working" else []
        )
        import asyncio as _asyncio
        self.mock_sleep.side_effect = [None, _asyncio.CancelledError()]
        try:
            await tasks.check_stalled_tasks()
        except _asyncio.CancelledError:
            pass
        self.mock_post_msg.assert_awaited()
        calls = [c for c in self.mock_post_msg.await_args_list
                 if c.kwargs.get("type") == "stall-warning"]
        assert len(calls) == 1

    async def test_no_stall_warning_for_active_client_task_below_threshold(self):
        """Fix 3: no stall warning when active-client task is idle <300s."""
        import tasks
        task = self._make_task("proj/fresh-1", idle_seconds=60, has_active_client=True)
        self.mock_list_tasks.side_effect = lambda status=None: (
            [task] if status == "working" else []
        )
        import asyncio as _asyncio
        self.mock_sleep.side_effect = [None, _asyncio.CancelledError()]
        try:
            await tasks.check_stalled_tasks()
        except _asyncio.CancelledError:
            pass
        stall_calls = [c for c in self.mock_post_msg.await_args_list
                       if c.kwargs.get("type") == "stall-warning"]
        assert len(stall_calls) == 0

    async def test_orphan_recovery_for_no_client_task(self):
        """Fix 3: orphan recovery triggers for no-client task idle >120s."""
        import tasks
        task = self._make_task("proj/orphan-1", idle_seconds=200, has_active_client=False)
        task_obj = dict(task, session_id="s1")
        self.mock_get_task.return_value = task_obj
        self.mock_list_tasks.side_effect = lambda status=None: (
            [task] if status == "working" else []
        )
        import asyncio as _asyncio
        self.mock_sleep.side_effect = [None, _asyncio.CancelledError()]
        try:
            await tasks.check_stalled_tasks()
        except _asyncio.CancelledError:
            pass
        self.mock_recover.assert_awaited_once()

    async def test_no_recovery_for_no_client_task_below_orphan_threshold(self):
        """Fix 3: no recovery for no-client task idle <=120s (not dead yet)."""
        import tasks
        task = self._make_task("proj/recent-1", idle_seconds=30, has_active_client=False)
        self.mock_list_tasks.side_effect = lambda status=None: (
            [task] if status == "working" else []
        )
        import asyncio as _asyncio
        self.mock_sleep.side_effect = [None, _asyncio.CancelledError()]
        try:
            await tasks.check_stalled_tasks()
        except _asyncio.CancelledError:
            pass
        self.mock_recover.assert_not_awaited()

    async def test_stall_not_triggered_for_no_client_task(self):
        """Fix 3: stall warning does NOT fire for no-client tasks (even if idle >300s)."""
        import tasks
        task = self._make_task("proj/orphan-stale", idle_seconds=400, has_active_client=False)
        task_obj = dict(task, session_id="s1")
        self.mock_get_task.return_value = task_obj
        self.mock_list_tasks.side_effect = lambda status=None: (
            [task] if status == "working" else []
        )
        import asyncio as _asyncio
        self.mock_sleep.side_effect = [None, _asyncio.CancelledError()]
        try:
            await tasks.check_stalled_tasks()
        except _asyncio.CancelledError:
            pass
        stall_calls = [c for c in self.mock_post_msg.await_args_list
                       if c.kwargs.get("type") == "stall-warning"]
        assert len(stall_calls) == 0


# ---------------------------------------------------------------------------
# _ensure_branch_pushed — git push logic
# ---------------------------------------------------------------------------

class TestEnsureBranchPushed:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_run = AsyncMock()
        self.mock_post_msg = AsyncMock()
        self.mock_exists = lambda p: True  # worktree always exists in tests

        patches = [
            patch("tasks._run_as_worker", self.mock_run),
            patch("tasks.db.post_task_message", self.mock_post_msg),
            patch("tasks.os.path.exists", side_effect=self.mock_exists),
        ]
        for p in patches:
            p.start()
        self._patches = patches
        yield
        for p in patches:
            p.stop()

    async def test_no_worktree_noop(self):
        from tasks import _ensure_branch_pushed
        await _ensure_branch_pushed("t1", {"worktree_path": None, "branch": "feat"})
        self.mock_run.assert_not_awaited()

    async def test_no_branch_noop(self):
        from tasks import _ensure_branch_pushed
        await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": None})
        self.mock_run.assert_not_awaited()

    async def test_nothing_to_push(self):
        from tasks import _ensure_branch_pushed
        # ls-remote returns a ref (remote exists), log shows nothing unpushed
        self.mock_run.side_effect = [
            (b"abc123\trefs/heads/feat\n", b"", 0),  # ls-remote
            (b"", b"", 0),  # log shows nothing unpushed
        ]
        await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": "feat"})
        assert self.mock_run.await_count == 2  # ls-remote + log, no push

    async def test_pushes_unpushed_commits(self):
        from tasks import _ensure_branch_pushed
        self.mock_run.side_effect = [
            (b"abc123\trefs/heads/feat\n", b"", 0),  # ls-remote
            (b"abc Fix something\n", b"", 0),  # log shows unpushed
            (b"", b"", 0),  # push succeeds
        ]
        await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": "feat"})
        assert self.mock_run.await_count == 3
        push_call = self.mock_run.await_args_list[2]
        assert "push" in push_call.args
        assert "--force-with-lease" in push_call.args

    async def test_pushes_when_no_remote_branch(self):
        from tasks import _ensure_branch_pushed
        self.mock_run.side_effect = [
            (b"", b"", 0),  # ls-remote returns empty (no remote branch)
            (b"", b"", 0),  # push succeeds
        ]
        await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": "feat"})
        assert self.mock_run.await_count == 2
        push_call = self.mock_run.await_args_list[1]
        assert "push" in push_call.args

    async def test_push_failure_posts_message(self):
        from tasks import _ensure_branch_pushed
        self.mock_run.side_effect = [
            (b"", b"", 0),  # ls-remote empty
            (b"", b"rejected", 1),  # push fails
        ]
        await _ensure_branch_pushed("t1", {"worktree_path": "/work/x", "branch": "feat"})
        self.mock_post_msg.assert_awaited_once()
        call_kwargs = self.mock_post_msg.await_args.kwargs
        assert call_kwargs["type"] == "status"
        assert "Auto-push failed" in call_kwargs["title"]


# ---------------------------------------------------------------------------
# _build_task_prompt — prompt construction
# ---------------------------------------------------------------------------

class TestBuildTaskPrompt:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_get_task = AsyncMock(return_value=None)
        self.mock_read_msgs = AsyncMock(return_value={"messages": []})

        patches = [
            patch("tasks.db.get_task", self.mock_get_task),
            patch("tasks.db.read_task_messages", self.mock_read_msgs),
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
        from tasks import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(), self._make_task(), "do the thing")
        assert "push your branch" in result.lower()

    async def test_auto_test_tells_cc_not_to_run_tests(self):
        from tasks import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(), self._make_task(auto_test=True), "do the thing")
        assert "automatically" in result.lower()

    async def test_no_auto_test_includes_test_command(self):
        from tasks import _build_task_prompt
        result = await _build_task_prompt(
            self._make_project(test_command="php artisan test"),
            self._make_task(), "do the thing")
        assert "php artisan test" in result

    async def test_dependency_context_included(self):
        from tasks import _build_task_prompt
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


# ---------------------------------------------------------------------------
# _rebase_and_redispatch — rebase logic (mocked git)
# ---------------------------------------------------------------------------

class TestRebaseAndRedispatch:
    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_run = AsyncMock()
        self.mock_update_task = AsyncMock()
        self.mock_post_msg = AsyncMock()
        self.mock_dispatch = AsyncMock()

        patches = [
            patch("tasks._run_as_worker", self.mock_run),
            patch("tasks.db.update_task", self.mock_update_task),
            patch("tasks.db.post_task_message", self.mock_post_msg),
            patch("tasks.dispatch_task", self.mock_dispatch),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_successful_rebase(self):
        from tasks import _rebase_and_redispatch
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

        # Should dispatch
        self.mock_dispatch.assert_awaited_once()

    async def test_rebase_conflict_aborts_and_dispatches(self):
        from tasks import _rebase_and_redispatch
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

        # Should still dispatch (CC handles conflicts)
        self.mock_dispatch.assert_awaited_once()


# ---------------------------------------------------------------------------
# web_push — dispatch logic
# ---------------------------------------------------------------------------

class TestWebPushDispatch:
    """Tests for web_push.dispatch_notification — settings checks, dispatch routing."""

    @pytest.fixture(autouse=True)
    def _enable_vapid(self, monkeypatch):
        """Patch VAPID keys so is_enabled() returns True."""
        import web_push
        monkeypatch.setattr(web_push, "VAPID_PRIVATE_KEY", "fake-private-key")
        monkeypatch.setattr(web_push, "VAPID_PUBLIC_KEY", "fake-public-key")

    @pytest.fixture
    def mock_settings(self):
        return {
            "id": 1,
            "notify_failed": True,
            "notify_needs_review": True,
            "notify_completed": False,
            "notify_question": True,
        }

    @pytest.fixture
    def one_subscription(self):
        return [{"endpoint": "https://push.example.com/sub1", "p256dh": "key1", "auth": "auth1"}]

    async def test_failed_triggers_notification(self, mock_settings, one_subscription):
        with patch("web_push.db.get_notification_settings", AsyncMock(return_value=mock_settings)), \
             patch("web_push.db.get_push_subscriptions", AsyncMock(return_value=one_subscription)), \
             patch("web_push._send_one", return_value=True) as mock_send:
            import web_push
            await web_push.dispatch_notification("failed", "proj/task-a", "✕ failed", "Error msg")
        mock_send.assert_called_once()
        payload = mock_send.call_args[0][1]
        import json
        payload_dict = json.loads(payload)
        assert payload_dict["title"] == "✕ failed"
        assert payload_dict["tag"] == "task-proj/task-a"
        assert "proj/task-a" in payload_dict["data"]["url"]

    async def test_completed_off_by_default_no_dispatch(self, mock_settings, one_subscription):
        """notify_completed=False → no push sent."""
        assert mock_settings["notify_completed"] is False
        with patch("web_push.db.get_notification_settings", AsyncMock(return_value=mock_settings)), \
             patch("web_push.db.get_push_subscriptions", AsyncMock(return_value=one_subscription)), \
             patch("web_push._send_one", return_value=True) as mock_send:
            import web_push
            await web_push.dispatch_notification("completed", "proj/task-a", "✓ done", "Summary")
        mock_send.assert_not_called()

    async def test_completed_on_dispatches(self, mock_settings, one_subscription):
        """notify_completed=True → push sent."""
        mock_settings["notify_completed"] = True
        with patch("web_push.db.get_notification_settings", AsyncMock(return_value=mock_settings)), \
             patch("web_push.db.get_push_subscriptions", AsyncMock(return_value=one_subscription)), \
             patch("web_push._send_one", return_value=True) as mock_send:
            import web_push
            await web_push.dispatch_notification("completed", "proj/task-a", "✓ done", "Summary")
        mock_send.assert_called_once()

    async def test_question_triggers_notification(self, mock_settings, one_subscription):
        with patch("web_push.db.get_notification_settings", AsyncMock(return_value=mock_settings)), \
             patch("web_push.db.get_push_subscriptions", AsyncMock(return_value=one_subscription)), \
             patch("web_push._send_one", return_value=True) as mock_send:
            import web_push
            await web_push.dispatch_notification("question", "proj/task-a", "❓ question", "What?")
        mock_send.assert_called_once()

    async def test_needs_review_triggers_notification(self, mock_settings, one_subscription):
        with patch("web_push.db.get_notification_settings", AsyncMock(return_value=mock_settings)), \
             patch("web_push.db.get_push_subscriptions", AsyncMock(return_value=one_subscription)), \
             patch("web_push._send_one", return_value=True) as mock_send:
            import web_push
            await web_push.dispatch_notification("needs_review", "proj/task-a", "⚠ needs review", "Timeout")
        mock_send.assert_called_once()

    async def test_no_subscriptions_no_crash(self, mock_settings):
        """Empty subscriptions list → no crash, returns 0."""
        with patch("web_push.db.get_notification_settings", AsyncMock(return_value=mock_settings)), \
             patch("web_push.db.get_push_subscriptions", AsyncMock(return_value=[])):
            import web_push
            count = await web_push.send_notification({"title": "test", "body": "body", "tag": "t"})
        assert count == 0

    async def test_send_failure_does_not_raise(self, mock_settings, one_subscription):
        """pywebpush raising an exception should not propagate."""
        with patch("web_push.db.get_notification_settings", AsyncMock(return_value=mock_settings)), \
             patch("web_push.db.get_push_subscriptions", AsyncMock(return_value=one_subscription)), \
             patch("web_push._send_one", return_value=False):
            import web_push
            # Should not raise
            await web_push.dispatch_notification("failed", "proj/task-a", "Failed", "Error")

    async def test_vapid_disabled_skips_dispatch(self, monkeypatch, mock_settings, one_subscription):
        """No VAPID keys → dispatch is a no-op."""
        import web_push
        monkeypatch.setattr(web_push, "VAPID_PRIVATE_KEY", "")
        monkeypatch.setattr(web_push, "VAPID_PUBLIC_KEY", "")
        with patch("web_push.db.get_notification_settings", AsyncMock(return_value=mock_settings)), \
             patch("web_push.db.get_push_subscriptions", AsyncMock(return_value=one_subscription)), \
             patch("web_push._send_one", return_value=True) as mock_send:
            await web_push.dispatch_notification("failed", "proj/task-a", "Failed", "Error")
        mock_send.assert_not_called()

    async def test_settings_error_does_not_raise(self, one_subscription):
        """DB error in get_notification_settings → dispatch swallows the exception."""
        with patch("web_push.db.get_notification_settings", AsyncMock(side_effect=Exception("DB error"))), \
             patch("web_push.db.get_push_subscriptions", AsyncMock(return_value=one_subscription)), \
             patch("web_push._send_one", return_value=True) as mock_send:
            import web_push
            await web_push.dispatch_notification("failed", "proj/task-a", "Failed", "Error")
        mock_send.assert_not_called()

    async def test_multiple_subscriptions_all_receive(self, mock_settings):
        """Multiple subs → each receives a push."""
        subs = [
            {"endpoint": "https://push.example.com/sub1", "p256dh": "k1", "auth": "a1"},
            {"endpoint": "https://push.example.com/sub2", "p256dh": "k2", "auth": "a2"},
            {"endpoint": "https://push.example.com/sub3", "p256dh": "k3", "auth": "a3"},
        ]
        with patch("web_push.db.get_notification_settings", AsyncMock(return_value=mock_settings)), \
             patch("web_push.db.get_push_subscriptions", AsyncMock(return_value=subs)), \
             patch("web_push._send_one", return_value=True) as mock_send:
            import web_push
            count = await web_push.dispatch_notification("failed", "proj/task-a", "Failed", "Err")
        assert mock_send.call_count == 3

    async def test_is_enabled_false_without_keys(self, monkeypatch):
        import web_push
        monkeypatch.setattr(web_push, "VAPID_PRIVATE_KEY", "")
        monkeypatch.setattr(web_push, "VAPID_PUBLIC_KEY", "")
        assert web_push.is_enabled() is False

    async def test_is_enabled_true_with_keys(self):
        import web_push
        assert web_push.is_enabled() is True


# ---------------------------------------------------------------------------
# _is_binary — binary file detection
# ---------------------------------------------------------------------------

class TestIsBinary:
    def setup_method(self):
        from server import _is_binary
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
        from server import _validate_path
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
        import server as _server
        # Clear module-level fetch cache so tests don't interfere with each other
        _server._fetch_cache.clear()

        self.mock_get_task = AsyncMock()
        self.mock_get_project = AsyncMock()
        self.mock_git_run = AsyncMock()
        self.mock_isdir = patch("os.path.isdir").start()

        patches = [
            patch("server.db.get_task", self.mock_get_task),
            patch("server.db.get_project", self.mock_get_project),
            patch("server._git_run", self.mock_git_run),
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
        from server import _handle_list_task_files
        self.mock_get_task.return_value = self._make_task(worktree_path="/work/proj/my-task")
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = True
        self.mock_git_run.return_value = (b"README.md\nserver.py\n", 0)

        result = await _handle_list_task_files({"task_id": "proj/my-task"})

        assert result["files"] == ["README.md", "server.py"]
        assert result["ref_used"] == "HEAD"
        assert "git_dir" not in result

    async def test_released_task_uses_origin_branch(self):
        from server import _handle_list_task_files
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
        from server import _handle_list_task_files
        self.mock_get_task.return_value = self._make_task(worktree_path=None, branch=None, status="cancelled")
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = False

        result = await _handle_list_task_files({"task_id": "proj/my-task"})

        assert "error" in result
        assert "not accessible" in result["error"]

    async def test_task_not_found(self):
        from server import _handle_list_task_files
        self.mock_get_task.return_value = None

        result = await _handle_list_task_files({"task_id": "proj/nonexistent"})

        assert "error" in result
        assert "not found" in result["error"]

    async def test_path_traversal_rejected(self):
        from server import _handle_list_task_files
        self.mock_get_task.return_value = self._make_task(worktree_path="/work/proj/my-task")
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = True

        result = await _handle_list_task_files({"task_id": "proj/my-task", "path": "../etc"})

        assert "error" in result
        assert ".." in result["error"]

    async def test_recursive_flag_passed(self):
        from server import _handle_list_task_files
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
        import server as _server
        _server._fetch_cache.clear()

        self.mock_get_task = AsyncMock()
        self.mock_get_project = AsyncMock()
        self.mock_git_run = AsyncMock()
        self.mock_isdir = patch("os.path.isdir").start()

        patches = [
            patch("server.db.get_task", self.mock_get_task),
            patch("server.db.get_project", self.mock_get_project),
            patch("server._git_run", self.mock_git_run),
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
        from server import _handle_get_task_file
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
        from server import _handle_get_task_file
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
        from server import _handle_get_task_file
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
        from server import _handle_get_task_file
        self.mock_get_task.return_value = self._make_task()
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = True
        self.mock_git_run.return_value = (b"", 128)

        result = await _handle_get_task_file({"task_id": "proj/my-task", "path": "nonexistent.py"})

        assert "error" in result
        assert "not found" in result["error"]

    async def test_path_traversal_rejected(self):
        from server import _handle_get_task_file
        self.mock_get_task.return_value = self._make_task()
        self.mock_get_project.return_value = self._make_project()

        result = await _handle_get_task_file({
            "task_id": "proj/my-task",
            "path": "../../etc/shadow",
        })

        assert "error" in result
        assert ".." in result["error"]

    async def test_inaccessible_task(self):
        from server import _handle_get_task_file
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
        from server import _handle_get_task_file
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
        from server import _handle_get_task_file
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
        from server import _git_run

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
        import server as _server
        _server._fetch_cache.clear()

        self.mock_get_task = AsyncMock()
        self.mock_get_project = AsyncMock()
        self.mock_git_run = AsyncMock()
        self.mock_isdir = patch("os.path.isdir").start()

        patches = [
            patch("server.db.get_task", self.mock_get_task),
            patch("server.db.get_project", self.mock_get_project),
            patch("server._git_run", self.mock_git_run),
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
        from server import _handle_list_task_files
        import server as _server

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
        from server import _handle_list_task_files
        import server as _server

        self.mock_get_task.return_value = self._make_task()
        self.mock_get_project.return_value = self._make_project()
        self.mock_isdir.return_value = False

        bare_path = "/work/proj/.bare"
        # Pre-seed cache with a stale timestamp
        _server._fetch_cache[bare_path] = time.monotonic() - (_server._FETCH_TTL + 1.0)

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
        import server as _server
        _server._fetch_cache.clear()

        self.mock_git_run = AsyncMock()
        self.mock_isdir = patch("os.path.isdir").start()
        patch("server._git_run", self.mock_git_run).start()
        yield
        patch.stopall()

    def _make_task(self, worktree_path=None, branch="feat/my-feature"):
        return {"worktree_path": worktree_path, "branch": branch}

    def _make_project(self):
        return {"working_dir": "/work/proj"}

    async def test_active_worktree_returns_head(self):
        """Priority 1: active worktree on disk → (worktree_path, HEAD)."""
        from server import _resolve_git_ref
        self.mock_isdir.return_value = True
        task = self._make_task(worktree_path="/work/proj/worktrees/my-task")

        result = await _resolve_git_ref(task, self._make_project())

        assert result == ("/work/proj/worktrees/my-task", "HEAD")
        self.mock_git_run.assert_not_called()

    async def test_branch_on_origin_returns_bare_ref(self):
        """Priority 2: no worktree, branch exists on origin → (bare_path, origin/branch)."""
        from server import _resolve_git_ref
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
        from server import _resolve_git_ref
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
        from server import _resolve_git_ref
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
        import server as _server
        from server import _resolve_git_ref

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
        assert bare_path not in _server._fetch_cache

        result2 = await _resolve_git_ref(task, project)
        assert result2 == (bare_path, "origin/feat/my-feature")
        # Cache should now be set after successful fetch
        assert bare_path in _server._fetch_cache
