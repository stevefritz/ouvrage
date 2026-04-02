"""Tests for setup_worktree handling of reopened tasks with existing remote branches."""

import os
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

import switchboard.db as db


class TestSetupWorktreeReopenedTask:
    """When a task is reopened and its branch exists on origin, setup_worktree
    should base the new worktree on origin/{branch} instead of origin/main."""

    @pytest.fixture(autouse=True)
    def _patches(self):
        self.run_calls = []

        async def fake_run(*cmd, **kwargs):
            self.run_calls.append(cmd)
            cmd_str = " ".join(cmd)

            # git symbolic-ref HEAD → refs/heads/main
            if "symbolic-ref" in cmd and "HEAD" in cmd:
                return b"refs/heads/main\n", b"", 0

            # git rev-parse --verify origin/{branch} → controls whether remote branch exists
            if "rev-parse" in cmd and "--verify" in cmd:
                ref = cmd[-1]
                if ref == "origin/existing-branch":
                    return b"abc123\n", b"", 0
                return b"", b"fatal: not found\n", 128

            # git worktree add -b ... → success
            if "worktree" in cmd and "add" in cmd:
                return b"", b"", 0

            # git fetch, config, mkdir, etc → success
            return b"", b"", 0

        self.run_mock = AsyncMock(side_effect=fake_run)
        self.patcher = patch("switchboard.git.worktree._run_as_worker", self.run_mock)
        self.patcher.start()
        # Patch _resolve_push_url to avoid DB lookups
        self.push_url_patcher = patch(
            "switchboard.git.operations._resolve_push_url",
            AsyncMock(side_effect=ValueError("no PAT")),
        )
        self.push_url_patcher.start()
        yield
        self.patcher.stop()
        self.push_url_patcher.stop()

    def _project(self, tmp_path):
        bare_path = tmp_path / ".bare"
        bare_path.mkdir()
        return {
            "id": "test-project",
            "repo": "https://github.com/test/repo.git",
            "working_dir": str(tmp_path),
            "default_branch": "main",
        }

    async def test_reopened_task_uses_remote_branch(self, tmp_path):
        """Branch exists on origin → base_ref should be origin/{branch}."""
        from switchboard.git.worktree import setup_worktree

        project = self._project(tmp_path)
        await setup_worktree(project, "existing-branch", "existing-branch")

        # Find the worktree add call
        worktree_add_calls = [c for c in self.run_calls if "worktree" in c and "add" in c]
        assert len(worktree_add_calls) == 1

        call = worktree_add_calls[0]
        # Should use origin/existing-branch as base, not origin/main
        assert call[-1] == "origin/existing-branch", (
            f"Expected base_ref 'origin/existing-branch' but got '{call[-1]}'"
        )

    async def test_new_task_uses_default_branch(self, tmp_path):
        """Branch does NOT exist on origin → base_ref should be origin/main."""
        from switchboard.git.worktree import setup_worktree

        project = self._project(tmp_path)
        await setup_worktree(project, "new-branch", "new-branch")

        worktree_add_calls = [c for c in self.run_calls if "worktree" in c and "add" in c]
        assert len(worktree_add_calls) == 1

        call = worktree_add_calls[0]
        assert call[-1] == "origin/main", (
            f"Expected base_ref 'origin/main' but got '{call[-1]}'"
        )

    async def test_depends_on_overrides_remote_branch(self, db, sample_project, tmp_path):
        """Even if origin/{branch} exists, depends_on takes priority."""
        from switchboard.git.worktree import setup_worktree

        # Create parent task with a branch
        await db.create_task(
            id="test-project/parent-task",
            project_id="test-project",
            goal="Parent",
            branch="parent-branch",
        )

        project = self._project(tmp_path)
        await setup_worktree(
            project, "existing-branch", "existing-branch",
            depends_on="test-project/parent-task",
        )

        worktree_add_calls = [c for c in self.run_calls if "worktree" in c and "add" in c]
        assert len(worktree_add_calls) == 1

        call = worktree_add_calls[0]
        # depends_on should win over the remote branch, using origin/ prefix
        assert call[-1] == "origin/parent-branch", (
            f"Expected base_ref 'origin/parent-branch' (from depends_on) but got '{call[-1]}'"
        )
