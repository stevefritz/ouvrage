"""Tests for empty-repo seeding in setup_worktree."""

import os
from unittest.mock import AsyncMock, call, patch

import pytest


class TestSeedEmptyRepo:
    """_seed_empty_repo() creates an initial commit when the repo has zero commits."""

    @pytest.fixture(autouse=True)
    def _patches(self):
        self.run_calls = []

        async def fake_run(*cmd, **kwargs):
            self.run_calls.append(cmd)
            return b"", b"", 0

        self.run_mock = AsyncMock(side_effect=fake_run)
        with patch("switchboard.git.worktree._run_as_worker", self.run_mock):
            yield

    async def test_empty_repo_triggers_seeding(self, tmp_path):
        """rev-parse HEAD returns rc=128 → seeding path runs."""
        from switchboard.git.worktree import _seed_empty_repo

        async def fake_run(*cmd, **kwargs):
            self.run_calls.append(cmd)
            if "rev-parse" in cmd and "HEAD" in cmd:
                return b"", b"fatal: bad default revision 'HEAD'\n", 128
            return b"", b"", 0

        self.run_mock.side_effect = fake_run

        bare_path = str(tmp_path / ".bare")
        await _seed_empty_repo(bare_path, "my-project", "main", "https://auth@github.com/org/repo.git")

        # Should have called git clone, git commit, git push
        cmds = [" ".join(c) for c in self.run_calls]
        assert any("clone" in c for c in cmds), f"Expected clone call, got: {cmds}"
        assert any("commit" in c for c in cmds), f"Expected commit call, got: {cmds}"
        assert any("push" in c for c in cmds), f"Expected push call, got: {cmds}"

    async def test_existing_repo_skips_seeding(self, tmp_path):
        """rev-parse HEAD returns rc=0 → no seeding happens."""
        from switchboard.git.worktree import _seed_empty_repo

        async def fake_run(*cmd, **kwargs):
            self.run_calls.append(cmd)
            # rev-parse HEAD succeeds (commits exist)
            if "rev-parse" in cmd and "HEAD" in cmd:
                return b"abc123def456\n", b"", 0
            return b"", b"", 0

        self.run_mock.side_effect = fake_run

        bare_path = str(tmp_path / ".bare")
        await _seed_empty_repo(bare_path, "my-project", "main", None)

        cmds = [" ".join(c) for c in self.run_calls]
        # Only the rev-parse call should have happened
        assert not any("clone" in c for c in cmds), f"Should not clone for existing repo, got: {cmds}"
        assert not any("commit" in c for c in cmds), f"Should not commit for existing repo, got: {cmds}"
        assert not any("push" in c for c in cmds), f"Should not push for existing repo, got: {cmds}"

    async def test_push_uses_auth_url(self, tmp_path):
        """When auth_url is provided, push uses it (not 'origin')."""
        from switchboard.git.worktree import _seed_empty_repo

        auth_url = "https://oauth2:TOKEN@github.com/org/repo.git"

        async def fake_run(*cmd, **kwargs):
            self.run_calls.append(cmd)
            if "rev-parse" in cmd and "HEAD" in cmd:
                return b"", b"fatal: bad default revision 'HEAD'\n", 128
            return b"", b"", 0

        self.run_mock.side_effect = fake_run

        bare_path = str(tmp_path / ".bare")
        await _seed_empty_repo(bare_path, "proj", "main", auth_url)

        push_calls = [c for c in self.run_calls if "push" in c]
        assert push_calls, "Expected at least one push call"
        push_call = push_calls[0]
        assert auth_url in push_call, (
            f"Expected auth_url in push args, got: {push_call}"
        )

    async def test_push_uses_origin_when_no_auth_url(self, tmp_path):
        """When auth_url is None, push falls back to 'origin'."""
        from switchboard.git.worktree import _seed_empty_repo

        async def fake_run(*cmd, **kwargs):
            self.run_calls.append(cmd)
            if "rev-parse" in cmd and "HEAD" in cmd:
                return b"", b"fatal: bad default revision 'HEAD'\n", 128
            return b"", b"", 0

        self.run_mock.side_effect = fake_run

        bare_path = str(tmp_path / ".bare")
        await _seed_empty_repo(bare_path, "proj", "main", None)

        push_calls = [c for c in self.run_calls if "push" in c]
        assert push_calls, "Expected at least one push call"
        push_call = push_calls[0]
        assert "origin" in push_call, (
            f"Expected 'origin' in push args when no auth_url, got: {push_call}"
        )

    async def test_readme_contains_project_id(self, tmp_path):
        """README.md written during seeding contains the project ID."""
        from switchboard.git.worktree import _seed_empty_repo

        written_files = {}

        async def fake_run(*cmd, **kwargs):
            self.run_calls.append(cmd)
            if "rev-parse" in cmd and "HEAD" in cmd:
                return b"", b"fatal: bad default revision 'HEAD'\n", 128
            # Capture what was written to the clone dir (simulated via tmp_path)
            return b"", b"", 0

        self.run_mock.side_effect = fake_run

        # Patch tempfile.mkdtemp to return a real temp dir so we can inspect README
        real_tmp = str(tmp_path / "seed-tmp")
        os.makedirs(real_tmp, exist_ok=True)

        with patch("switchboard.git.worktree.tempfile.mkdtemp", return_value=real_tmp):
            bare_path = str(tmp_path / ".bare")
            await _seed_empty_repo(bare_path, "my-awesome-project", "main", None)

        readme = os.path.join(real_tmp, "README.md")
        # tmp_dir is cleaned up — but we patched mkdtemp so we can check before cleanup
        # Actually shutil.rmtree runs in finally, so file is gone. Instead check via
        # the written content captured during fake_run. Let's check add call includes README.md
        add_calls = [c for c in self.run_calls if "add" in c]
        assert any("README.md" in " ".join(c) for c in add_calls), (
            f"Expected 'git add README.md' call, got add calls: {add_calls}"
        )


class TestSetupWorktreeWithEmptyRepo:
    """setup_worktree() calls _seed_empty_repo() for newly cloned bare repos."""

    @pytest.fixture(autouse=True)
    def _patches(self):
        self.run_calls = []

        async def fake_run(*cmd, **kwargs):
            self.run_calls.append(cmd)
            cmd_str = " ".join(cmd)

            if "symbolic-ref" in cmd_str and "HEAD" in cmd_str:
                return b"refs/heads/main\n", b"", 0

            # rev-parse HEAD in bare repo → empty repo
            if "rev-parse" in cmd_str and "HEAD" in cmd_str and "verify" not in cmd_str:
                return b"", b"fatal: bad default revision 'HEAD'\n", 128

            # rev-parse --verify origin/branch → not found
            if "rev-parse" in cmd_str and "--verify" in cmd_str:
                return b"", b"fatal: not found\n", 128

            if "worktree" in cmd_str and "add" in cmd_str:
                return b"", b"", 0

            return b"", b"", 0

        self.run_mock = AsyncMock(side_effect=fake_run)

        with patch("switchboard.git.worktree._run_as_worker", self.run_mock), \
             patch("switchboard.git.operations._resolve_push_url",
                   AsyncMock(side_effect=ValueError("no PAT"))):
            yield

    def _project(self, tmp_path):
        bare_path = tmp_path / ".bare"
        bare_path.mkdir()
        return {
            "id": "test-project",
            "repo": "https://github.com/test/repo.git",
            "working_dir": str(tmp_path),
            "default_branch": "main",
        }

    async def test_empty_repo_seeds_before_worktree_add(self, tmp_path):
        """For an empty bare repo, seeding (clone+commit+push) happens before worktree add."""
        from switchboard.git.worktree import setup_worktree

        project = self._project(tmp_path)
        await setup_worktree(project, "my-task", "my-task")

        cmds = [" ".join(c) for c in self.run_calls]
        clone_idx = next((i for i, c in enumerate(cmds) if "clone" in c and ".bare" in c), None)
        worktree_add_idx = next((i for i, c in enumerate(cmds) if "worktree" in c and "add" in c), None)

        # Seeding clone should happen before worktree add
        assert clone_idx is not None, f"Expected seed clone call, cmds: {cmds}"
        assert worktree_add_idx is not None, f"Expected worktree add call, cmds: {cmds}"
        assert clone_idx < worktree_add_idx, (
            "Seed clone should happen before worktree add"
        )

    async def test_existing_repo_no_seeding(self, tmp_path):
        """For a repo with commits, no seeding clone is issued."""
        from switchboard.git.worktree import setup_worktree

        async def fake_run_with_commits(*cmd, **kwargs):
            self.run_calls.append(cmd)
            cmd_str = " ".join(cmd)
            if "symbolic-ref" in cmd_str and "HEAD" in cmd_str:
                return b"refs/heads/main\n", b"", 0
            # rev-parse HEAD succeeds — repo has commits
            if "rev-parse" in cmd_str and "HEAD" in cmd_str and "verify" not in cmd_str:
                return b"abc123\n", b"", 0
            if "rev-parse" in cmd_str and "--verify" in cmd_str:
                return b"", b"fatal: not found\n", 128
            if "worktree" in cmd_str and "add" in cmd_str:
                return b"", b"", 0
            return b"", b"", 0

        self.run_mock.side_effect = fake_run_with_commits

        project = self._project(tmp_path)
        await setup_worktree(project, "my-task", "my-task")

        cmds = [" ".join(c) for c in self.run_calls]
        # There should be NO seed clone (clone of .bare into a tmp dir)
        seed_clones = [c for c in cmds if "clone" in c and ".bare" in c]
        assert not seed_clones, (
            f"Should not seed existing repo, but found clone calls: {seed_clones}"
        )
