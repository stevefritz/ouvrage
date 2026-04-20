"""Tests for empty-repo seeding in setup_worktree."""

import io
import os
from unittest.mock import AsyncMock, patch

import pytest

_FAKE_TMP = "/tmp/ouvrage-seed-testXXXX"


def _make_fake_run(run_calls, *, empty_repo=True, mktemp_path=_FAKE_TMP):
    """Return a fake _run_as_worker that records calls and returns sensible defaults.

    Args:
        empty_repo: If True, rev-parse HEAD returns rc=128 (empty repo).
                    If False, returns rc=0 (commits exist).
        mktemp_path: Path to return for `mktemp -d` calls.
    """
    async def fake_run(*cmd, **kwargs):
        run_calls.append(cmd)
        cmd_str = " ".join(cmd)

        # mktemp -d → return a controlled tmp path so README.md is never
        # written relative to the current directory
        if cmd[0] == "mktemp":
            return mktemp_path.encode() + b"\n", b"", 0

        # rev-parse HEAD (no --verify) → empty or non-empty repo
        if "rev-parse" in cmd and "HEAD" in cmd and "--verify" not in cmd:
            if empty_repo:
                return b"", b"fatal: bad default revision 'HEAD'\n", 128
            return b"abc123def456\n", b"", 0

        return b"", b"", 0

    return fake_run


class TestSeedEmptyRepo:
    """_seed_empty_repo() creates an initial commit when the repo has zero commits."""

    @pytest.fixture(autouse=True)
    def _patches(self):
        self.run_calls = []
        self.run_mock = AsyncMock(side_effect=_make_fake_run(self.run_calls))
        with patch("ouvrage.git.worktree._run_as_worker", self.run_mock), \
             patch("builtins.open", side_effect=self._fake_open):
            yield

    def _fake_open(self, path, *args, **kwargs):
        """Intercept open() calls to avoid writing to the real filesystem."""
        self._last_opened = path
        f = io.StringIO()
        f.__enter__ = lambda s: s
        f.__exit__ = lambda s, *a: None
        return f

    async def test_empty_repo_triggers_seeding(self, tmp_path):
        """rev-parse HEAD returns rc=128 → seeding path runs."""
        from ouvrage.git.worktree import _seed_empty_repo

        self.run_mock.side_effect = _make_fake_run(self.run_calls, empty_repo=True)

        bare_path = str(tmp_path / ".bare")
        await _seed_empty_repo(bare_path, "my-project", "main", "https://auth@github.com/org/repo.git")

        cmds = [" ".join(c) for c in self.run_calls]
        assert any("clone" in c for c in cmds), f"Expected clone call, got: {cmds}"
        assert any("commit" in c for c in cmds), f"Expected commit call, got: {cmds}"
        assert any("push" in c for c in cmds), f"Expected push call, got: {cmds}"

    async def test_existing_repo_skips_seeding(self, tmp_path):
        """rev-parse HEAD returns rc=0 → no seeding happens."""
        from ouvrage.git.worktree import _seed_empty_repo

        self.run_mock.side_effect = _make_fake_run(self.run_calls, empty_repo=False)

        bare_path = str(tmp_path / ".bare")
        await _seed_empty_repo(bare_path, "my-project", "main", None)

        cmds = [" ".join(c) for c in self.run_calls]
        assert not any("clone" in c for c in cmds), f"Should not clone for existing repo, got: {cmds}"
        assert not any("commit" in c for c in cmds), f"Should not commit for existing repo, got: {cmds}"
        assert not any("push" in c for c in cmds), f"Should not push for existing repo, got: {cmds}"

    async def test_push_uses_auth_url(self, tmp_path):
        """When auth_url is provided, push uses it (not 'origin')."""
        from ouvrage.git.worktree import _seed_empty_repo

        auth_url = "https://oauth2:TOKEN@github.com/org/repo.git"
        self.run_mock.side_effect = _make_fake_run(self.run_calls, empty_repo=True)

        bare_path = str(tmp_path / ".bare")
        await _seed_empty_repo(bare_path, "proj", "main", auth_url)

        push_calls = [c for c in self.run_calls if "push" in c]
        assert push_calls, "Expected at least one push call"
        assert auth_url in push_calls[0], (
            f"Expected auth_url in push args, got: {push_calls[0]}"
        )

    async def test_push_uses_origin_when_no_auth_url(self, tmp_path):
        """When auth_url is None, push falls back to 'origin'."""
        from ouvrage.git.worktree import _seed_empty_repo

        self.run_mock.side_effect = _make_fake_run(self.run_calls, empty_repo=True)

        bare_path = str(tmp_path / ".bare")
        await _seed_empty_repo(bare_path, "proj", "main", None)

        push_calls = [c for c in self.run_calls if "push" in c]
        assert push_calls, "Expected at least one push call"
        assert "origin" in push_calls[0], (
            f"Expected 'origin' in push args when no auth_url, got: {push_calls[0]}"
        )

    async def test_readme_contains_project_id(self, tmp_path):
        """README.md written during seeding contains the project ID."""
        from ouvrage.git.worktree import _seed_empty_repo

        written_content = {}

        class CapturingFile(io.StringIO):
            def __enter__(self):
                return self
            def __exit__(self, *args):
                written_content["readme"] = self.getvalue()

        self.run_mock.side_effect = _make_fake_run(self.run_calls, empty_repo=True)

        with patch("builtins.open", return_value=CapturingFile()):
            bare_path = str(tmp_path / ".bare")
            await _seed_empty_repo(bare_path, "my-awesome-project", "main", None)

        assert "readme" in written_content, "open() was not called for README.md"
        assert "my-awesome-project" in written_content["readme"], (
            f"project_id not in README content: {written_content['readme']!r}"
        )


class TestSetupWorktreeWithEmptyRepo:
    """setup_worktree() calls _seed_empty_repo() for newly cloned bare repos."""

    @pytest.fixture(autouse=True)
    def _patches(self):
        self.run_calls = []

        async def fake_run(*cmd, **kwargs):
            self.run_calls.append(cmd)
            cmd_str = " ".join(cmd)

            if cmd[0] == "mktemp":
                return b"/tmp/ouvrage-seed-testXXXX\n", b"", 0

            if "symbolic-ref" in cmd_str and "HEAD" in cmd_str:
                return b"refs/heads/main\n", b"", 0

            # rev-parse HEAD (no --verify) → empty repo
            if "rev-parse" in cmd_str and "HEAD" in cmd_str and "verify" not in cmd_str:
                return b"", b"fatal: bad default revision 'HEAD'\n", 128

            # rev-parse --verify origin/branch → not found
            if "rev-parse" in cmd_str and "--verify" in cmd_str:
                return b"", b"fatal: not found\n", 128

            if "worktree" in cmd_str and "add" in cmd_str:
                return b"", b"", 0

            return b"", b"", 0

        self.run_mock = AsyncMock(side_effect=fake_run)

        with patch("ouvrage.git.worktree._run_as_worker", self.run_mock), \
             patch("ouvrage.git.operations._resolve_push_url",
                   AsyncMock(side_effect=ValueError("no PAT"))), \
             patch("builtins.open", side_effect=self._fake_open):
            yield

    def _fake_open(self, path, *args, **kwargs):
        """Intercept open() to prevent writes to the real filesystem."""
        f = io.StringIO()
        f.__enter__ = lambda s: s
        f.__exit__ = lambda s, *a: None
        return f

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
        from ouvrage.git.worktree import setup_worktree

        project = self._project(tmp_path)
        await setup_worktree(project, "my-task", "my-task")

        cmds = [" ".join(c) for c in self.run_calls]
        clone_idx = next((i for i, c in enumerate(cmds) if "clone" in c and ".bare" in c), None)
        worktree_add_idx = next((i for i, c in enumerate(cmds) if "worktree" in c and "add" in c), None)

        assert clone_idx is not None, f"Expected seed clone call, cmds: {cmds}"
        assert worktree_add_idx is not None, f"Expected worktree add call, cmds: {cmds}"
        assert clone_idx < worktree_add_idx, "Seed clone should happen before worktree add"

    async def test_existing_repo_no_seeding(self, tmp_path):
        """For a repo with commits, no seeding clone is issued."""
        from ouvrage.git.worktree import setup_worktree

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
        seed_clones = [c for c in cmds if "clone" in c and ".bare" in c]
        assert not seed_clones, (
            f"Should not seed existing repo, but found clone calls: {seed_clones}"
        )
