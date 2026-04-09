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


    async def test_component_base_branch_wins_over_depends_on(self, db, sample_project):
        """component.base_branch is used even when depends_on is set."""
        from switchboard.git.operations import resolve_branch_target

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


# ---------------------------------------------------------------------------
# Auto-merge flow
# ---------------------------------------------------------------------------

class TestAutoMerge:
    """_perform_auto_merge: merge, push, conflict handling."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mock_run = AsyncMock(return_value=(b"", b"", 0))
        self.mock_resolve_url = AsyncMock(return_value="https://oauth2:ghp_test@github.com/acme/widgets.git")
        patches = [
            patch("switchboard.git.operations._run_as_worker", self.mock_run),
            patch("switchboard.git.operations._resolve_push_url", self.mock_resolve_url),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()


    async def test_merge_conflict(self, db, sample_project):
        """Merge conflict: status=needs-review, conflict files listed."""
        from switchboard.git.operations import _perform_auto_merge

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


    async def test_detached_head_avoids_branch_conflict(self, db, sample_project):
        """Detached HEAD approach never checks out branch by name — no worktree conflict possible."""
        from switchboard.git.operations import _perform_auto_merge

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

        with patch("switchboard.dispatch.engine.release_worktree", AsyncMock()) as mock_release:
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


    async def test_push_retry_exhausted(self, db, sample_project):
        """Push retry: fails all 3 attempts → needs-review."""
        from switchboard.git.operations import _perform_auto_merge

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


# ---------------------------------------------------------------------------
# Worktree lifecycle
# ---------------------------------------------------------------------------

class TestWorktreeLifecycle:
    """Worktree detach, reattach, and release."""

    async def test_release_worktree(self, db, sample_project):
        """release_worktree sets worktree_path to NULL."""
        from switchboard.dispatch.engine import release_worktree

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
        from switchboard.dispatch.engine import release_worktree

        task = await db.create_task(
            id="test-project/no-wt", project_id="test-project",
            goal="No worktree",
        )

        result = await release_worktree("test-project/no-wt")
        assert result["released"] is False

    async def test_release_worktree_not_found(self, db, sample_project):
        """release_worktree raises for unknown task."""
        from switchboard.dispatch.engine import release_worktree

        with pytest.raises(ValueError, match="not found"):
            await release_worktree("test-project/nonexistent")


    async def test_no_auto_release_when_disabled(self, db, sample_project):
        """auto_release_worktree=false skips release."""
        from switchboard.dispatch.engine import _auto_release_worktree

        task = await db.create_task(
            id="test-project/keep-wt", project_id="test-project",
            goal="Keep worktree", auto_release_worktree=False,
        )
        await db.update_task(task["id"], worktree_path="/tmp/fake-worktree")

        # Need to update auto_release_worktree — it's set at create time but
        # let's also update it to make sure the field works via update
        await db.update_task(task["id"], auto_release_worktree=False)

        with patch("switchboard.dispatch.engine.release_worktree", AsyncMock()) as mock_release:
            await _auto_release_worktree("test-project/keep-wt")
            mock_release.assert_not_awaited()


# ---------------------------------------------------------------------------
# Blocking error messages
# ---------------------------------------------------------------------------

class TestBlockingErrors:
    """Worktree creation shows helpful error when branch is held."""

    async def test_find_branch_holder(self, db, sample_project):
        """_find_branch_holder returns the holding task info."""
        from switchboard.git.worktree import _find_branch_holder

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


    async def test_find_branch_holder_null_worktree(self, db, sample_project):
        """_find_branch_holder ignores tasks with NULL worktree_path."""
        from switchboard.git.worktree import _find_branch_holder

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


# ---------------------------------------------------------------------------
# _check_and_dispatch_dependents integration with auto-merge
# ---------------------------------------------------------------------------

class TestCheckAndDispatchWithAutoMerge:
    """_check_and_dispatch_dependents calls auto-merge when enabled."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_lifecycle_execute = AsyncMock()
        self.mock_pr = AsyncMock()
        self.mock_drain = AsyncMock()

        patches = [
            patch("switchboard.dispatch.lifecycle.lifecycle.execute", self.mock_lifecycle_execute),
            patch("switchboard.dispatch.engine._maybe_create_pr", self.mock_pr),
            patch("switchboard.dispatch.engine._drain_queue", self.mock_drain),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_auto_merge_called_on_gate_pass(self, db, sample_project):
        """When auto_merge is true, _perform_auto_merge is called."""
        from switchboard.dispatch.engine import _check_and_dispatch_dependents

        task = await db.create_task(
            id="test-project/am-gate", project_id="test-project",
            goal="Auto merge gate", auto_merge=True,
        )
        await db.update_task(task["id"],
            status="completed", gate_status="passed", gate_passed_at=db.now_iso(),
            worktree_path="/tmp/fake",
        )

        with patch("switchboard.dispatch.engine._perform_auto_merge", AsyncMock(return_value=True)) as mock_merge:
            with patch("switchboard.dispatch.engine._auto_release_worktree", AsyncMock()):
                await _check_and_dispatch_dependents("test-project/am-gate")
                mock_merge.assert_awaited_once_with("test-project/am-gate")


