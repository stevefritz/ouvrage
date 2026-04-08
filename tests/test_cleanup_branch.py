"""Tests for branch cleanup in cleanup_worktree() for terminal tasks."""

import os
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


class TestCleanupBranchOnWorktreeRelease:
    """cleanup_worktree() should force-delete the local branch for terminal tasks."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        # Create a fake bare repo directory so os.path.exists(bare_path) is True
        self.bare_path = tmp_path / ".bare"
        self.bare_path.mkdir()

        self.project = {
            "id": "test-project",
            "working_dir": str(tmp_path),
        }

    def _task(self, status: str, branch: str = "feature/my-task", worktree_path: str | None = None):
        return {
            "id": "task-123",
            "status": status,
            "branch": branch,
            "worktree_path": worktree_path,
        }

    async def test_branch_deleted_for_completed_task(self):
        """Terminal (completed) task → branch is force-deleted with -D."""
        from switchboard.git.worktree import cleanup_worktree

        task = self._task("completed", branch="feature/done")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)) as mock_exec:
            await cleanup_worktree(self.project, task)

        # Find the branch delete call
        branch_calls = [
            c for c in mock_exec.call_args_list
            if "branch" in c.args
        ]
        assert len(branch_calls) == 1, f"Expected 1 branch call, got: {branch_calls}"
        assert "-D" in branch_calls[0].args, (
            f"Expected -D flag for completed task, got: {branch_calls[0].args}"
        )
        assert "feature/done" in branch_calls[0].args

    async def test_branch_deleted_for_cancelled_task(self):
        """Terminal (cancelled) task → branch is force-deleted with -D."""
        from switchboard.git.worktree import cleanup_worktree

        task = self._task("cancelled", branch="feature/cancelled")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)) as mock_exec:
            await cleanup_worktree(self.project, task)

        branch_calls = [c for c in mock_exec.call_args_list if "branch" in c.args]
        assert len(branch_calls) == 1
        assert "-D" in branch_calls[0].args

    async def test_branch_not_force_deleted_for_stopped_task(self):
        """Non-terminal (stopped) task → branch uses safe -d, NOT force -D."""
        from switchboard.git.worktree import cleanup_worktree

        task = self._task("stopped", branch="feature/paused")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)) as mock_exec:
            await cleanup_worktree(self.project, task)

        branch_calls = [c for c in mock_exec.call_args_list if "branch" in c.args]
        assert len(branch_calls) == 1
        assert "-D" not in branch_calls[0].args, (
            "Stopped task must NOT use -D (branch may be needed for resume)"
        )
        assert "-d" in branch_calls[0].args

    async def test_graceful_on_missing_branch_for_terminal_task(self, caplog):
        """Terminal task with already-deleted branch → no exception, warning logged."""
        import logging
        from switchboard.git.worktree import cleanup_worktree

        task = self._task("completed", branch="feature/already-gone")

        mock_proc = MagicMock()
        mock_proc.returncode = 128  # git branch -D fails — branch not found
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"error: branch 'feature/already-gone' not found.\n")
        )

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_proc)):
            with caplog.at_level(logging.WARNING, logger="switchboard.git.worktree"):
                # Must not raise
                await cleanup_worktree(self.project, task)

        assert any("feature/already-gone" in r.message for r in caplog.records), (
            "Expected a warning log mentioning the branch name"
        )
