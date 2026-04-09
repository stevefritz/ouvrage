"""Tier 2: Integration tests — real SQLite DB + real git repos, no CC sessions.

Tests database operations, git operations, and state machine transitions
with actual infrastructure but no Claude Code subprocess.
"""

import asyncio
import os
import subprocess

import pytest

# Ensure git uses 'main' as default branch in test repos
_GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t.com",
             "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t.com"}


# ---------------------------------------------------------------------------
# Database: subtask CRUD
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Database: task dependencies and chain queries
# ---------------------------------------------------------------------------

class TestDependencyQueries:
    async def _seed_chain(self, db):
        """Create project + A -> B -> C chain."""
        await db.create_project(
            id="chain-proj", repo="git@github.com:test/chain.git",
            working_dir="/tmp/chain",
        )
        await db.create_task(id="task-a", project_id="chain-proj", goal="A")
        await db.create_task(id="task-b", project_id="chain-proj", goal="B",
                             depends_on="task-a")
        await db.create_task(id="task-c", project_id="chain-proj", goal="C",
                             depends_on="task-b")


    async def test_chain_from_middle(self, db):
        await self._seed_chain(db)
        chain = await db.get_chain("task-b")
        ids = [t["id"] for t in chain]
        assert "task-a" in ids
        assert "task-b" in ids
        assert "task-c" in ids


# ---------------------------------------------------------------------------
# Database: gate status transitions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Database: task messages for review flow
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Git: push enforcement with real repos
# ---------------------------------------------------------------------------

class TestPushEnforcementGit:
    """Real git operations — create local repos, test push logic."""

    @pytest.fixture
    def git_repos(self, tmp_path):
        """Create a bare remote + a worktree clone with an unpushed commit."""
        remote = tmp_path / "remote.git"
        worktree = tmp_path / "worktree"

        def run(args):
            subprocess.run(args, check=True, capture_output=True, env=_GIT_ENV)

        # Create bare remote with main as default branch
        run(["git", "init", "--bare", "--initial-branch=main", str(remote)])

        # Clone into worktree
        run(["git", "clone", str(remote), str(worktree)])

        # Initial commit on main so we have a branch
        (worktree / "README.md").write_text("# Test\n")
        run(["git", "-C", str(worktree), "add", "."])
        run(["git", "-C", str(worktree), "commit", "-m", "init"])
        run(["git", "-C", str(worktree), "push", "origin", "main"])

        # Create feature branch with unpushed commit
        run(["git", "-C", str(worktree), "checkout", "-b", "feat-test"])
        (worktree / "new_file.py").write_text("print('hello')\n")
        run(["git", "-C", str(worktree), "add", "."])
        run(["git", "-C", str(worktree), "commit", "-m", "add feature"])

        return {"remote": str(remote), "worktree": str(worktree), "branch": "feat-test"}

    def test_unpushed_commit_detected(self, git_repos):
        """Verify our test setup has an unpushed commit."""
        result = subprocess.run(
            ["git", "-C", git_repos["worktree"], "log",
             "origin/feat-test..HEAD", "--oneline"],
            capture_output=True, text=True,
        )
        # origin/feat-test doesn't exist, so this will error or show commits
        # Just verify the branch has commits not on remote
        result2 = subprocess.run(
            ["git", "-C", git_repos["worktree"], "ls-remote", "--heads",
             "origin", "feat-test"],
            capture_output=True, text=True,
        )
        assert result2.stdout.strip() == ""  # remote branch doesn't exist yet

    def test_push_creates_remote_branch(self, git_repos):
        """Verify manual push works (sanity check for the fixture)."""
        subprocess.run(
            ["git", "-C", git_repos["worktree"], "push", "origin", "feat-test"],
            check=True, capture_output=True,
        )
        result = subprocess.run(
            ["git", "-C", git_repos["worktree"], "ls-remote", "--heads",
             "origin", "feat-test"],
            capture_output=True, text=True,
        )
        assert "feat-test" in result.stdout


# ---------------------------------------------------------------------------
# Git: rebase operations
# ---------------------------------------------------------------------------

class TestRebaseGit:
    """Real git rebase scenarios."""

    @pytest.fixture
    def diverged_repos(self, tmp_path):
        """Create a remote with two diverged branches (for rebase testing)."""
        remote = tmp_path / "remote.git"
        parent_wt = tmp_path / "parent"
        child_wt = tmp_path / "child"

        def run(args, cwd=None):
            subprocess.run(args, check=True, capture_output=True, cwd=cwd, env=_GIT_ENV)

        # Bare remote
        run(["git", "init", "--bare", "--initial-branch=main", str(remote)])

        # Parent worktree with initial commit
        run(["git", "clone", str(remote), str(parent_wt)])
        (parent_wt / "base.txt").write_text("base\n")
        run(["git", "-C", str(parent_wt), "add", "."])
        run(["git", "-C", str(parent_wt), "commit", "-m", "base"])
        run(["git", "-C", str(parent_wt), "push", "origin", "main"])

        # Create parent branch
        run(["git", "-C", str(parent_wt), "checkout", "-b", "feat-parent"])
        (parent_wt / "parent_file.txt").write_text("parent v1\n")
        run(["git", "-C", str(parent_wt), "add", "."])
        run(["git", "-C", str(parent_wt), "commit", "-m", "parent work"])
        run(["git", "-C", str(parent_wt), "push", "origin", "feat-parent"])

        # Child worktree branches from parent
        run(["git", "clone", str(remote), str(child_wt)])
        run(["git", "-C", str(child_wt), "checkout", "-b", "feat-child", "origin/feat-parent"])
        (child_wt / "child_file.txt").write_text("child work\n")
        run(["git", "-C", str(child_wt), "add", "."])
        run(["git", "-C", str(child_wt), "commit", "-m", "child work"])
        run(["git", "-C", str(child_wt), "push", "origin", "feat-child"])

        # Now parent adds more commits (simulating retry + new work)
        (parent_wt / "parent_file.txt").write_text("parent v2 (updated)\n")
        run(["git", "-C", str(parent_wt), "add", "."])
        run(["git", "-C", str(parent_wt), "commit", "-m", "parent updated"])
        run(["git", "-C", str(parent_wt), "push", "origin", "feat-parent"])

        return {
            "remote": str(remote),
            "parent_wt": str(parent_wt),
            "child_wt": str(child_wt),
            "parent_branch": "feat-parent",
            "child_branch": "feat-child",
        }

    def test_clean_rebase(self, diverged_repos):
        """Child can rebase onto updated parent without conflicts."""
        child = diverged_repos["child_wt"]
        parent_branch = diverged_repos["parent_branch"]

        subprocess.run(
            ["git", "-C", child, "fetch", "origin"], check=True, capture_output=True,
        )
        result = subprocess.run(
            ["git", "-C", child, "rebase", f"origin/{parent_branch}"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0

        # Verify child has parent's updated file
        content = (subprocess.run(
            ["git", "-C", child, "show", "HEAD:parent_file.txt"],
            capture_output=True, text=True,
        )).stdout
        assert "parent v2" in content

    def test_conflicting_rebase(self, diverged_repos):
        """When child modified same file as parent, rebase conflicts."""
        child = diverged_repos["child_wt"]
        parent_branch = diverged_repos["parent_branch"]

        # Child also modifies parent_file.txt (conflict!)
        with open(os.path.join(child, "parent_file.txt"), "w") as f:
            f.write("child changed parent file\n")
        subprocess.run(["git", "-C", child, "add", "."], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", child, "commit", "-m", "conflict"],
                       check=True, capture_output=True, env=_GIT_ENV)

        subprocess.run(
            ["git", "-C", child, "fetch", "origin"], check=True, capture_output=True,
        )
        result = subprocess.run(
            ["git", "-C", child, "rebase", f"origin/{parent_branch}"],
            capture_output=True, text=True,
        )
        assert result.returncode != 0  # Should fail

        # Abort the rebase
        subprocess.run(
            ["git", "-C", child, "rebase", "--abort"],
            check=True, capture_output=True,
        )


# ---------------------------------------------------------------------------
# Dashboard API: subtasks in task detail
# ---------------------------------------------------------------------------

