"""Tests for auto-merge, branch resolution, worktree lifecycle."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# resolve_branch_target — config inheritance
# ---------------------------------------------------------------------------

class TestResolveBranchTarget:
    """Branch target resolution: task.base_branch > component.base_branch > project default.

    depends_on has NO influence on merge target.
    """

    async def test_depends_on_does_not_affect_branch_target(self, db, sample_project):
        """depends_on must NOT affect branch target — task.base_branch wins."""
        from tasks import resolve_branch_target

        parent = await db.create_task(
            id="test-project/parent", project_id="test-project",
            goal="Parent", branch="feature/parent-branch",
        )
        child = await db.create_task(
            id="test-project/child", project_id="test-project",
            goal="Child", depends_on="test-project/parent",
            base_branch="staging",
        )

        result = await resolve_branch_target(child)
        # Must return task's own base_branch, never the parent's branch
        assert result == "staging"
        assert result != "feature/parent-branch"

    async def test_depends_on_resolves_to_project_default(self, db, sample_project):
        """depends_on with no other config falls back to project.default_branch, not parent branch."""
        from tasks import resolve_branch_target

        parent = await db.create_task(
            id="test-project/parent-a", project_id="test-project",
            goal="Parent A", branch="task-a-branch",
        )
        child = await db.create_task(
            id="test-project/child-b", project_id="test-project",
            goal="Child B", depends_on="test-project/parent-a",
        )

        result = await resolve_branch_target(child)
        assert result == "main"       # project default_branch
        assert result != "task-a-branch"  # must NOT be parent's branch

    async def test_uses_task_base_branch(self, db, sample_project):
        """task.base_branch is used when set."""
        from tasks import resolve_branch_target

        task = await db.create_task(
            id="test-project/explicit", project_id="test-project",
            goal="Explicit base", base_branch="staging",
        )

        result = await resolve_branch_target(task)
        assert result == "staging"

    async def test_uses_component_base_branch(self, db, sample_project):
        """component.base_branch used when task has none."""
        from tasks import resolve_branch_target

        comp = await db.create_component(
            id="test-project/api", project_id="test-project",
            name="API Layer", base_branch="develop",
        )
        task = await db.create_task(
            id="test-project/comp-task", project_id="test-project",
            goal="Component task", component_id="test-project/api",
        )

        result = await resolve_branch_target(task)
        assert result == "develop"

    async def test_falls_back_to_project_default(self, db, sample_project):
        """Falls back to project.default_branch."""
        from tasks import resolve_branch_target

        task = await db.create_task(
            id="test-project/basic", project_id="test-project",
            goal="Basic task",
        )

        result = await resolve_branch_target(task)
        assert result == "main"  # sample_project default_branch

    async def test_component_base_branch_wins_over_depends_on(self, db, sample_project):
        """component.base_branch is used even when depends_on is set."""
        from tasks import resolve_branch_target

        comp = await db.create_component(
            id="test-project/api", project_id="test-project",
            name="API", base_branch="develop",
        )
        parent = await db.create_task(
            id="test-project/parent", project_id="test-project",
            goal="Parent", branch="feature/parent",
        )
        child = await db.create_task(
            id="test-project/child", project_id="test-project",
            goal="Child", depends_on="test-project/parent",
            component_id="test-project/api",
        )

        result = await resolve_branch_target(child)
        # component base_branch wins; depends_on has no influence
        assert result == "develop"
        assert result != "feature/parent"

    async def test_depends_on_parent_merged_falls_to_project_default(self, db, sample_project):
        """depends_on child with no other config returns project default regardless of parent state."""
        from tasks import resolve_branch_target

        parent = await db.create_task(
            id="test-project/merged-parent", project_id="test-project",
            goal="Parent", branch="parent-branch",
        )
        await db.update_task("test-project/merged-parent",
            status="merged", gate_status="passed",
            gate_passed_at=db.now_iso(), worktree_path=None,
        )
        child = await db.create_task(
            id="test-project/orphan-child", project_id="test-project",
            goal="Child", depends_on="test-project/merged-parent",
        )

        result = await resolve_branch_target(child)
        assert result == "main"

    async def test_depends_on_parent_with_worktree_still_uses_project_default(self, db, sample_project):
        """depends_on child returns project default even when parent's worktree still exists."""
        from tasks import resolve_branch_target

        parent = await db.create_task(
            id="test-project/wt-parent", project_id="test-project",
            goal="Parent", branch="wt-parent-branch",
        )
        await db.update_task("test-project/wt-parent",
            status="completed", gate_status="passed",
            gate_passed_at=db.now_iso(), worktree_path="/work/test/wt-parent",
        )
        child = await db.create_task(
            id="test-project/wt-child", project_id="test-project",
            goal="Child", depends_on="test-project/wt-parent",
        )

        result = await resolve_branch_target(child)
        # depends_on must never pollute merge target
        assert result == "main"
        assert result != "wt-parent-branch"


# ---------------------------------------------------------------------------
# Auto-merge flow
# ---------------------------------------------------------------------------

class TestAutoMerge:
    """_perform_auto_merge: merge, push, conflict handling."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mock_run = AsyncMock(return_value=(b"", b"", 0))
        patches = [
            patch("tasks._run_as_worker", self.mock_run),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_merge_success(self, db, sample_project):
        """Successful merge: status=merged, pushed_at set."""
        from tasks import _perform_auto_merge

        task = await db.create_task(
            id="test-project/merge-ok", project_id="test-project",
            goal="Merge OK", auto_merge=True,
        )
        await db.update_task(task["id"],
            status="completed", worktree_path="/tmp/fake-worktree",
        )

        result = await _perform_auto_merge("test-project/merge-ok")
        assert result is True

        updated = await db.get_task("test-project/merge-ok")
        assert updated["status"] == "merged"
        assert updated["pushed_at"] is not None
        assert updated["pr_status"] == "merged"
        assert updated["branch_target"] == "main"  # project default

    async def test_merge_conflict(self, db, sample_project):
        """Merge conflict: status=needs-review, conflict files listed."""
        from tasks import _perform_auto_merge

        task = await db.create_task(
            id="test-project/merge-conflict", project_id="test-project",
            goal="Conflict", auto_merge=True,
        )
        await db.update_task(task["id"],
            status="completed", worktree_path="/tmp/fake-worktree",
        )

        # Make merge fail, diff --name-only returns conflict files
        call_count = 0
        async def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            cmd = " ".join(args)
            if "merge" in cmd and "--no-edit" in cmd:
                return (b"", b"CONFLICT", 1)
            if "diff" in cmd and "--name-only" in cmd:
                return (b"file1.py\nfile2.py\n", b"", 0)
            return (b"", b"", 0)

        self.mock_run.side_effect = mock_run

        result = await _perform_auto_merge("test-project/merge-conflict")
        assert result is False

        updated = await db.get_task("test-project/merge-conflict")
        assert updated["status"] == "needs-review"
        assert updated["pr_status"] == "conflict"
        assert "file1.py" in updated["pr_error"]

    async def test_merge_sets_branch_target(self, db, sample_project):
        """branch_target is resolved and stored on the task."""
        from tasks import _perform_auto_merge

        task = await db.create_task(
            id="test-project/target-test", project_id="test-project",
            goal="Target test", auto_merge=True, base_branch="staging",
        )
        await db.update_task(task["id"],
            status="completed", worktree_path="/tmp/fake-worktree",
        )

        await _perform_auto_merge("test-project/target-test")

        updated = await db.get_task("test-project/target-test")
        assert updated["branch_target"] == "staging"

    async def test_detached_head_avoids_branch_conflict(self, db, sample_project):
        """Detached HEAD approach never checks out branch by name — no worktree conflict possible."""
        from tasks import _perform_auto_merge

        task = await db.create_task(
            id="test-project/detach-test", project_id="test-project",
            goal="Detached HEAD test", auto_merge=True,
        )
        await db.update_task(task["id"],
            status="completed", worktree_path="/tmp/fake-worktree",
            branch="feature/my-branch",
        )

        cmds_called = []
        async def mock_run(*args, **kwargs):
            cmds_called.append(list(args))
            return (b"", b"", 0)

        self.mock_run.side_effect = mock_run

        with patch("tasks.release_worktree", AsyncMock()) as mock_release:
            result = await _perform_auto_merge("test-project/detach-test")

        assert result is True
        # Must use --detach, never a plain branch checkout of the target
        checkout_cmds = [a for a in cmds_called if "checkout" in a]
        assert any("--detach" in cmd for cmd in checkout_cmds), "Expected --detach in checkout commands"
        assert not any(
            "checkout" in " ".join(cmd) and "main" in cmd and "--detach" not in cmd
            for cmd in checkout_cmds
        ), "Should never checkout 'main' without --detach"
        # Push must use HEAD:main refspec, not plain branch name
        push_cmds = [a for a in cmds_called if "push" in a]
        assert any("HEAD:main" in " ".join(cmd) for cmd in push_cmds), "Expected HEAD:main push refspec"
        # release_worktree must never be called
        mock_release.assert_not_awaited()

    async def test_push_retry_succeeds(self, db, sample_project):
        """Push retry: fails on first attempt, succeeds on second."""
        from tasks import _perform_auto_merge

        task = await db.create_task(
            id="test-project/retry-ok", project_id="test-project",
            goal="Retry OK", auto_merge=True,
        )
        await db.update_task(task["id"],
            status="completed", worktree_path="/tmp/fake-worktree",
            branch="feature/retry",
        )

        push_count = 0
        async def mock_run(*args, **kwargs):
            nonlocal push_count
            cmd = " ".join(args)
            if "push" in cmd and "HEAD:" in cmd:
                push_count += 1
                if push_count == 1:
                    return (b"", b"rejected", 1)
                return (b"", b"", 0)
            return (b"", b"", 0)

        self.mock_run.side_effect = mock_run

        result = await _perform_auto_merge("test-project/retry-ok")
        assert result is True
        assert push_count == 2

        updated = await db.get_task("test-project/retry-ok")
        assert updated["status"] == "merged"

    async def test_push_retry_exhausted(self, db, sample_project):
        """Push retry: fails all 3 attempts → needs-review."""
        from tasks import _perform_auto_merge

        task = await db.create_task(
            id="test-project/retry-fail", project_id="test-project",
            goal="Retry fail", auto_merge=True,
        )
        await db.update_task(task["id"],
            status="completed", worktree_path="/tmp/fake-worktree",
            branch="feature/retry-fail",
        )

        push_count = 0
        async def mock_run(*args, **kwargs):
            nonlocal push_count
            cmd = " ".join(args)
            if "push" in cmd and "HEAD:" in cmd:
                push_count += 1
                return (b"", b"rejected", 1)
            return (b"", b"", 0)

        self.mock_run.side_effect = mock_run

        result = await _perform_auto_merge("test-project/retry-fail")
        assert result is False
        assert push_count == 3

        updated = await db.get_task("test-project/retry-fail")
        assert updated["status"] == "needs-review"
        assert updated["pr_status"] == "push-failed"

    async def test_merge_posts_message(self, db, sample_project):
        """Auto-merge posts a status message on success."""
        from tasks import _perform_auto_merge

        task = await db.create_task(
            id="test-project/msg-test", project_id="test-project",
            goal="Msg test", auto_merge=True,
        )
        await db.update_task(task["id"],
            status="completed", worktree_path="/tmp/fake-worktree",
        )

        await _perform_auto_merge("test-project/msg-test")

        msgs = await db.read_task_messages("test-project/msg-test")
        status_msgs = [m for m in msgs["messages"] if m["type"] == "status"]
        assert any("Auto-merged" in m["title"] for m in status_msgs)


# ---------------------------------------------------------------------------
# Worktree lifecycle
# ---------------------------------------------------------------------------

class TestWorktreeLifecycle:
    """Worktree detach, reattach, and release."""

    async def test_release_worktree(self, db, sample_project):
        """release_worktree sets worktree_path to NULL."""
        from tasks import release_worktree

        task = await db.create_task(
            id="test-project/release-me", project_id="test-project",
            goal="Release test",
        )
        await db.update_task(task["id"], worktree_path="/tmp/fake-worktree")

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_proc:
            mock_proc.return_value.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.return_value.returncode = 0

            result = await release_worktree("test-project/release-me")

        assert result["released"] is True
        updated = await db.get_task("test-project/release-me")
        assert updated["worktree_path"] is None

    async def test_release_worktree_no_worktree(self, db, sample_project):
        """release_worktree on task with no worktree returns released=False."""
        from tasks import release_worktree

        task = await db.create_task(
            id="test-project/no-wt", project_id="test-project",
            goal="No worktree",
        )

        result = await release_worktree("test-project/no-wt")
        assert result["released"] is False

    async def test_release_worktree_not_found(self, db, sample_project):
        """release_worktree raises for unknown task."""
        from tasks import release_worktree

        with pytest.raises(ValueError, match="not found"):
            await release_worktree("test-project/nonexistent")

    async def test_auto_release_on_gate_pass(self, db, sample_project):
        """auto_release_worktree=true triggers release after gate pass."""
        from tasks import _auto_release_worktree

        task = await db.create_task(
            id="test-project/auto-release", project_id="test-project",
            goal="Auto release", auto_release_worktree=True,
        )
        await db.update_task(task["id"], worktree_path="/tmp/fake-worktree")

        with patch("tasks.release_worktree", AsyncMock(return_value={"released": True})) as mock_release:
            await _auto_release_worktree("test-project/auto-release")
            mock_release.assert_awaited_once_with("test-project/auto-release", reason="completion")

    async def test_no_auto_release_when_disabled(self, db, sample_project):
        """auto_release_worktree=false skips release."""
        from tasks import _auto_release_worktree

        task = await db.create_task(
            id="test-project/keep-wt", project_id="test-project",
            goal="Keep worktree", auto_release_worktree=False,
        )
        await db.update_task(task["id"], worktree_path="/tmp/fake-worktree")

        # Need to update auto_release_worktree — it's set at create time but
        # let's also update it to make sure the field works via update
        await db.update_task(task["id"], auto_release_worktree=False)

        with patch("tasks.release_worktree", AsyncMock()) as mock_release:
            await _auto_release_worktree("test-project/keep-wt")
            mock_release.assert_not_awaited()


# ---------------------------------------------------------------------------
# Blocking error messages
# ---------------------------------------------------------------------------

class TestBlockingErrors:
    """Worktree creation shows helpful error when branch is held."""

    async def test_find_branch_holder(self, db, sample_project):
        """_find_branch_holder returns the holding task info."""
        from tasks import _find_branch_holder

        task = await db.create_task(
            id="test-project/holder", project_id="test-project",
            goal="Holding branch", branch="feature/shared",
        )
        await db.update_task(task["id"],
            status="completed", worktree_path="/work/test-project/holder",
        )

        result = await _find_branch_holder("feature/shared")
        assert result is not None
        assert result["task_id"] == "test-project/holder"
        assert result["status"] == "completed"
        assert result["worktree_path"] == "/work/test-project/holder"

    async def test_find_branch_holder_none(self, db, sample_project):
        """_find_branch_holder returns None when no holder."""
        from tasks import _find_branch_holder

        result = await _find_branch_holder("feature/nonexistent")
        assert result is None

    async def test_find_branch_holder_null_worktree(self, db, sample_project):
        """_find_branch_holder ignores tasks with NULL worktree_path."""
        from tasks import _find_branch_holder

        task = await db.create_task(
            id="test-project/released", project_id="test-project",
            goal="Released", branch="feature/released",
        )
        # worktree_path is None by default

        result = await _find_branch_holder("feature/released")
        assert result is None


# ---------------------------------------------------------------------------
# Schema: new fields on tasks
# ---------------------------------------------------------------------------

class TestNewFields:
    """New task fields are persisted and returned correctly."""

    async def test_create_task_with_auto_merge(self, db, sample_project):
        task = await db.create_task(
            id="test-project/merge-task", project_id="test-project",
            goal="Merge task", auto_merge=True, auto_release_worktree=True,
            base_branch="staging",
        )
        assert task["auto_merge"] is True
        assert task["auto_release_worktree"] is True
        assert task["base_branch"] == "staging"

    async def test_update_task_merge_fields(self, db, sample_project):
        task = await db.create_task(
            id="test-project/update-merge", project_id="test-project",
            goal="Update merge",
        )
        updated = await db.update_task(task["id"],
            branch_target="develop", pushed_at="2026-01-01T00:00:00Z",
            pr_status="merged", pr_error=None,
        )
        assert updated["branch_target"] == "develop"
        assert updated["pushed_at"] == "2026-01-01T00:00:00Z"
        assert updated["pr_status"] == "merged"

    async def test_queued_at_field(self, db, sample_project):
        task = await db.create_task(
            id="test-project/q-field", project_id="test-project",
            goal="Queue field test",
        )
        updated = await db.update_task(task["id"], queued_at="2026-01-01T00:00:00Z")
        assert updated["queued_at"] == "2026-01-01T00:00:00Z"

        # Clear it
        updated = await db.update_task(task["id"], queued_at=None)
        assert updated["queued_at"] is None


# ---------------------------------------------------------------------------
# _check_and_dispatch_dependents integration with auto-merge
# ---------------------------------------------------------------------------

class TestCheckAndDispatchWithAutoMerge:
    """_check_and_dispatch_dependents calls auto-merge when enabled."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_dispatch = AsyncMock()
        self.mock_pr = AsyncMock()
        self.mock_drain = AsyncMock()

        patches = [
            patch("tasks.dispatch_task", self.mock_dispatch),
            patch("tasks._maybe_create_pr", self.mock_pr),
            patch("tasks._drain_queue", self.mock_drain),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_auto_merge_called_on_gate_pass(self, db, sample_project):
        """When auto_merge is true, _perform_auto_merge is called."""
        from tasks import _check_and_dispatch_dependents

        task = await db.create_task(
            id="test-project/am-gate", project_id="test-project",
            goal="Auto merge gate", auto_merge=True,
        )
        await db.update_task(task["id"],
            status="completed", gate_status="passed", gate_passed_at=db.now_iso(),
            worktree_path="/tmp/fake",
        )

        with patch("tasks._perform_auto_merge", AsyncMock(return_value=True)) as mock_merge:
            with patch("tasks._auto_release_worktree", AsyncMock()):
                await _check_and_dispatch_dependents("test-project/am-gate")
                mock_merge.assert_awaited_once_with("test-project/am-gate")

    async def test_chain_stops_on_merge_failure(self, db, sample_project):
        """When auto-merge fails, dependents are NOT dispatched."""
        from tasks import _check_and_dispatch_dependents

        task = await db.create_task(
            id="test-project/am-fail", project_id="test-project",
            goal="Merge fail", auto_merge=True,
        )
        await db.update_task(task["id"],
            status="completed", gate_status="passed", gate_passed_at=db.now_iso(),
        )

        dep = await db.create_task(
            id="test-project/am-dep", project_id="test-project",
            goal="Dependent", depends_on="test-project/am-fail",
        )

        with patch("tasks._perform_auto_merge", AsyncMock(return_value=False)):
            with patch("tasks._auto_release_worktree", AsyncMock()):
                await _check_and_dispatch_dependents("test-project/am-fail")

        # Dependent should NOT have been dispatched
        self.mock_dispatch.assert_not_awaited()

    async def test_queue_drained_after_chain(self, db, sample_project):
        """_drain_queue is called at the end of _check_and_dispatch_dependents."""
        from tasks import _check_and_dispatch_dependents

        task = await db.create_task(
            id="test-project/drain-test", project_id="test-project",
            goal="Drain test",
        )
        await db.update_task(task["id"],
            status="completed", gate_status="passed", gate_passed_at=db.now_iso(),
        )

        with patch("tasks._perform_auto_merge", AsyncMock(return_value=True)):
            with patch("tasks._auto_release_worktree", AsyncMock()):
                await _check_and_dispatch_dependents("test-project/drain-test")

        self.mock_drain.assert_awaited_once()
