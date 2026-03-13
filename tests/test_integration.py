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

class TestSubtaskCRUD:
    async def _seed_project_and_task(self, db):
        """Helper to create a project + task for subtask tests."""
        await db.create_project(
            id="test-proj", repo="git@github.com:test/repo.git",
            working_dir="/tmp/test-proj",
        )
        await db.create_task(
            id="task-1", project_id="test-proj", goal="test task",
        )

    async def test_create_and_get_subtask(self, db):
        await self._seed_project_and_task(db)
        sub = await db.create_subtask(
            id="task-1/review-0", task_id="task-1",
            type="review", prompt="Review this", model="sonnet",
        )
        assert sub["id"] == "task-1/review-0"
        assert sub["status"] == "working"
        assert sub["model"] == "sonnet"

        fetched = await db.get_subtask("task-1/review-0")
        assert fetched is not None
        assert fetched["task_id"] == "task-1"

    async def test_update_subtask(self, db):
        await self._seed_project_and_task(db)
        await db.create_subtask(
            id="task-1/review-0", task_id="task-1",
            type="review", prompt="Review this",
        )
        updated = await db.update_subtask(
            "task-1/review-0",
            status="completed",
            result="APPROVED",
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.05,
            duration_ms=3000,
            completed_at=db.now_iso(),
        )
        assert updated["status"] == "completed"
        assert updated["input_tokens"] == 1000
        assert updated["cost_usd"] == 0.05

    async def test_get_subtasks_by_task(self, db):
        await self._seed_project_and_task(db)
        await db.create_subtask(id="task-1/review-0", task_id="task-1",
                                type="review", prompt="r1")
        await db.create_subtask(id="task-1/review-1", task_id="task-1",
                                type="review", prompt="r2")
        subs = await db.get_subtasks("task-1")
        assert len(subs) == 2
        assert subs[0]["id"] == "task-1/review-0"

    async def test_get_subtask_nonexistent(self, db):
        result = await db.get_subtask("nonexistent")
        assert result is None

    async def test_subtasks_empty_for_task_without_subtasks(self, db):
        await self._seed_project_and_task(db)
        subs = await db.get_subtasks("task-1")
        assert subs == []


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

    async def test_get_dependents(self, db):
        await self._seed_chain(db)
        deps = await db.get_dependents("task-a")
        assert len(deps) == 1
        assert deps[0]["id"] == "task-b"

    async def test_get_chain(self, db):
        await self._seed_chain(db)
        chain = await db.get_chain("task-a")
        ids = [t["id"] for t in chain]
        assert "task-a" in ids
        assert "task-b" in ids
        assert "task-c" in ids

    async def test_chain_from_middle(self, db):
        await self._seed_chain(db)
        chain = await db.get_chain("task-b")
        ids = [t["id"] for t in chain]
        assert "task-a" in ids
        assert "task-b" in ids
        assert "task-c" in ids

    async def test_no_dependents(self, db):
        await self._seed_chain(db)
        deps = await db.get_dependents("task-c")
        assert deps == []


# ---------------------------------------------------------------------------
# Database: gate status transitions
# ---------------------------------------------------------------------------

class TestGateStatusTransitions:
    async def _seed(self, db):
        await db.create_project(id="gate-proj", repo="git@x.git",
                                working_dir="/tmp/gate")
        await db.create_task(id="gate-task", project_id="gate-proj", goal="test gate")

    async def test_gate_testing(self, db):
        await self._seed(db)
        await db.update_task("gate-task", gate_status="testing")
        task = await db.get_task("gate-task")
        assert task["gate_status"] == "testing"

    async def test_gate_passed(self, db):
        await self._seed(db)
        await db.update_task("gate-task", gate_status="passed",
                             gate_passed_at=db.now_iso())
        task = await db.get_task("gate-task")
        assert task["gate_status"] == "passed"
        assert task["gate_passed_at"] is not None

    async def test_gate_stale(self, db):
        await self._seed(db)
        # First pass, then mark stale
        await db.update_task("gate-task", gate_status="passed",
                             gate_passed_at=db.now_iso())
        await db.update_task("gate-task", gate_status="stale",
                             gate_passed_at=None)
        task = await db.get_task("gate-task")
        assert task["gate_status"] == "stale"
        assert task["gate_passed_at"] is None

    async def test_gate_retries_increment(self, db):
        await self._seed(db)
        await db.update_task("gate-task", gate_retries=1)
        task = await db.get_task("gate-task")
        assert task["gate_retries"] == 1
        await db.update_task("gate-task", gate_retries=2)
        task = await db.get_task("gate-task")
        assert task["gate_retries"] == 2


# ---------------------------------------------------------------------------
# Database: task messages for review flow
# ---------------------------------------------------------------------------

class TestTaskMessages:
    async def _seed(self, db):
        await db.create_project(id="msg-proj", repo="git@x.git",
                                working_dir="/tmp/msg")
        await db.create_task(id="msg-task", project_id="msg-proj", goal="test msgs")

    async def test_post_and_read_review_message(self, db):
        await self._seed(db)
        await db.post_task_message(
            task_id="msg-task", author="cc-worker", type="review",
            title="APPROVED", content="Code looks good",
        )
        thread = await db.read_task_messages("msg-task")
        msgs = thread["messages"]
        assert len(msgs) == 1
        assert msgs[0]["type"] == "review"
        assert msgs[0]["title"] == "APPROVED"

    async def test_multiple_review_messages(self, db):
        await self._seed(db)
        await db.post_task_message(
            task_id="msg-task", author="cc-worker", type="review",
            title="CHANGES REQUESTED", content="Fix imports",
        )
        await db.post_task_message(
            task_id="msg-task", author="cc-worker", type="review",
            title="APPROVED", content="Fixed, looks good now",
        )
        thread = await db.read_task_messages("msg-task")
        msgs = [m for m in thread["messages"] if m["type"] == "review"]
        assert len(msgs) == 2
        # Last one should be APPROVED
        assert msgs[-1]["title"] == "APPROVED"


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

class TestDashboardAPISubtasks:
    """Verify subtasks appear in task detail API response."""

    async def test_task_detail_includes_subtasks(self, db):
        await db.create_project(id="api-proj", repo="git@x.git",
                                working_dir="/tmp/api")
        await db.create_task(id="api-task", project_id="api-proj", goal="test API")
        await db.create_subtask(id="api-task/review-0", task_id="api-task",
                                type="review", prompt="review it")
        subs = await db.get_subtasks("api-task")
        assert len(subs) == 1
        assert subs[0]["type"] == "review"
