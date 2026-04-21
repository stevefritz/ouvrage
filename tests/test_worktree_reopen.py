"""Tests for setup_worktree handling of reopened tasks with existing remote branches."""

import os
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

import ouvrage.db as db


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
        self.patcher = patch("ouvrage.git.worktree._run_as_worker", self.run_mock)
        self.patcher.start()
        # Patch _resolve_push_url to avoid DB lookups
        self.push_url_patcher = patch(
            "ouvrage.git.operations._resolve_push_url",
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
        from ouvrage.git.worktree import setup_worktree

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
        from ouvrage.git.worktree import setup_worktree

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
        from ouvrage.git.worktree import setup_worktree

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

    async def test_chained_task_persists_base_branch(self, db, sample_project, tmp_path):
        """When task_id is provided and depends_on resolves a parent branch,
        base_branch should be written to the task record."""
        from ouvrage.git.worktree import setup_worktree

        await db.create_task(
            id="test-project/parent-task",
            project_id="test-project",
            goal="Parent",
            branch="parent-branch",
        )
        await db.create_task(
            id="test-project/child-task",
            project_id="test-project",
            goal="Child",
            branch="child-branch",
        )

        project = self._project(tmp_path)
        await setup_worktree(
            project, "child-branch", "child-branch",
            depends_on="test-project/parent-task",
            task_id="test-project/child-task",
        )

        child = await db.get_task("test-project/child-task")
        assert child["base_branch"] == "parent-branch", (
            f"Expected base_branch='parent-branch' but got '{child['base_branch']}'"
        )

    async def test_standalone_task_base_branch_remains_none(self, db, sample_project, tmp_path):
        """A task with no depends_on should NOT get base_branch set — it falls
        back to project default_branch at review time."""
        from ouvrage.git.worktree import setup_worktree

        await db.create_task(
            id="test-project/standalone-task",
            project_id="test-project",
            goal="Standalone",
            branch="standalone-branch",
        )

        project = self._project(tmp_path)
        await setup_worktree(
            project, "standalone-branch", "standalone-branch",
            task_id="test-project/standalone-task",
        )

        task = await db.get_task("test-project/standalone-task")
        assert task["base_branch"] is None, (
            f"Expected base_branch=None for standalone task but got '{task['base_branch']}'"
        )
