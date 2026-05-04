"""Regression test: setup_worktree() heals extensions.worktreeConfig on bare repos.

Bug: extensions.worktreeConfig=true leftover from deleted credential helper code
(ffe5eb2) caused worktrees to inherit core.bare=true, breaking all git operations.
"""

import os
import subprocess
from unittest.mock import AsyncMock, patch

import pytest

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "t@t.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "t@t.com",
}


class TestWorktreeConfigHealing:
    """setup_worktree() unsets extensions.worktreeConfig from the bare repo."""

    async def test_worktree_config_healed_and_git_status_works(self, tmp_path):
        """After setup_worktree(), git status must succeed and the extension must be gone."""
        # Create a local source repo (simulates the remote)
        source = tmp_path / "source"
        source.mkdir()
        subprocess.run(["git", "init", str(source)], check=True,
                       capture_output=True, env=_GIT_ENV)
        subprocess.run(["git", "-C", str(source), "symbolic-ref", "HEAD", "refs/heads/main"],
                       check=True, capture_output=True, env=_GIT_ENV)
        (source / "README.md").write_text("# Test\n")
        subprocess.run(["git", "-C", str(source), "add", "."],
                       check=True, capture_output=True, env=_GIT_ENV)
        subprocess.run(["git", "-C", str(source), "commit", "-m", "init"],
                       check=True, capture_output=True, env=_GIT_ENV)

        # Create working_dir and a bare clone (simulates what setup_worktree creates)
        working_dir = tmp_path / "working"
        working_dir.mkdir()
        bare_path = working_dir / ".bare"
        subprocess.run(["git", "clone", "--bare", str(source), str(bare_path)],
                       check=True, capture_output=True, env=_GIT_ENV)
        subprocess.run(
            ["git", "-C", str(bare_path), "config",
             "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"],
            check=True, capture_output=True, env=_GIT_ENV,
        )
        subprocess.run(
            ["git", "-C", str(bare_path), "fetch", str(source),
             "+refs/heads/*:refs/remotes/origin/*"],
            check=True, capture_output=True, env=_GIT_ENV,
        )

        # Poison the bare config with the buggy extension
        subprocess.run(
            ["git", "-C", str(bare_path), "config", "extensions.worktreeConfig", "true"],
            check=True, capture_output=True, env=_GIT_ENV,
        )
        pre_fix = subprocess.run(
            ["git", "-C", str(bare_path), "config", "--get", "extensions.worktreeConfig"],
            capture_output=True,
        )
        assert pre_fix.returncode == 0, "extensions.worktreeConfig should be set before setup_worktree"

        project = {
            "id": "test-project",
            "repo": str(source),
            "working_dir": str(working_dir),
            "default_branch": "main",
        }

        from ouvrage.git.worktree import setup_worktree

        with patch("ouvrage.git.operations._resolve_push_url",
                   AsyncMock(side_effect=ValueError("no PAT"))):
            worktree_path = await setup_worktree(project, "test-branch", "test-branch")

        # git status must succeed (exit 0) in the resulting worktree
        status = subprocess.run(
            ["git", "-C", worktree_path, "status"],
            capture_output=True, env=_GIT_ENV,
        )
        assert status.returncode == 0, (
            f"git status failed in worktree — extensions.worktreeConfig not healed.\n"
            f"stderr: {status.stderr.decode()}"
        )

        # extensions.worktreeConfig must be gone from the bare config
        after_fix = subprocess.run(
            ["git", "-C", str(bare_path), "config", "--get", "extensions.worktreeConfig"],
            capture_output=True,
        )
        assert after_fix.returncode != 0, (
            "extensions.worktreeConfig still set in bare config after setup_worktree"
        )
