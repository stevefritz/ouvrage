"""Tests for git_push and git_fetch MCP worker tools."""

import json
import os
import subprocess
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# git_push tests
# ---------------------------------------------------------------------------


class TestGitPush:
    """Tests for the git_push MCP tool handler."""

    @pytest.fixture(autouse=True)
    def _patches(self, tmp_path):
        self.worktree = str(tmp_path / "wt")
        os.makedirs(self.worktree)
        self.mock_run = AsyncMock()
        self.mock_pat = AsyncMock(return_value="ghp_testtoken123")
        self.task = {
            "id": "proj/task1",
            "project_id": "proj",
            "branch": "my-branch",
            "worktree_path": self.worktree,
        }
        self.project = {
            "id": "proj",
            "repo": "https://github.com/org/repo.git",
            "working_dir": str(tmp_path),
        }
        self.patches = [
            patch("switchboard.server.handlers.git_tools._run_as_worker", self.mock_run),
            patch("switchboard.server.handlers.git_tools.get_github_pat", self.mock_pat),
            patch("switchboard.server.handlers.git_tools.db"),
        ]
        mocks = [p.start() for p in self.patches]
        self.mock_db = mocks[2]
        self.mock_db.get_task = AsyncMock(return_value=self.task)
        self.mock_db.get_project = AsyncMock(return_value=self.project)
        yield
        for p in self.patches:
            p.stop()

    async def test_push_success_with_commits(self):
        """git_push returns success when there are commits to push."""
        from switchboard.server.handlers.git_tools import _handle_git_push

        # rev-parse returns task branch
        # log returns unpushed commits
        # push succeeds
        # fetch origin succeeds
        self.mock_run.side_effect = [
            (b"my-branch\n", b"", 0),           # rev-parse --abbrev-ref HEAD
            (b"abc123 fix bug\ndef456 add test\n", b"", 0),  # log origin/branch..HEAD
            (b"", b"", 0),                       # push
            (b"", b"", 0),                       # fetch origin (tracking ref update)
        ]

        result = await _handle_git_push({"task_id": "proj/task1"})

        assert result["pushed"] is True
        assert result["branch"] == "my-branch"
        assert result["commits"] == 2

    async def test_push_nothing_to_push(self):
        """git_push returns message when nothing to push."""
        from switchboard.server.handlers.git_tools import _handle_git_push

        self.mock_run.side_effect = [
            (b"my-branch\n", b"", 0),  # rev-parse
            (b"", b"", 0),             # log — empty, nothing unpushed
        ]

        result = await _handle_git_push({"task_id": "proj/task1"})

        assert result["pushed"] is False
        assert "Nothing to push" in result["message"]

    async def test_push_rejects_wrong_branch(self):
        """git_push rejects push when current branch doesn't match task branch."""
        from switchboard.server.handlers.git_tools import _handle_git_push

        self.mock_run.side_effect = [
            (b"other-branch\n", b"", 0),  # rev-parse — wrong branch
        ]

        result = await _handle_git_push({"task_id": "proj/task1"})

        assert result["pushed"] is False
        assert result["error"] == "wrong_branch"
        assert "other-branch" in result["message"]

    async def test_push_divergence_error(self):
        """git_push returns structured error on push rejection (divergence)."""
        from switchboard.server.handlers.git_tools import _handle_git_push

        self.mock_run.side_effect = [
            (b"my-branch\n", b"", 0),                        # rev-parse
            (b"abc123 fix\n", b"", 0),                       # log
            (b"", b"rejected non-fast-forward\n", 1),        # push fails
        ]

        result = await _handle_git_push({"task_id": "proj/task1"})

        assert result["pushed"] is False
        assert result["error"] == "push_rejected"
        assert "git_fetch" in result["message"]

    async def test_push_task_not_found(self):
        """git_push returns error for unknown task."""
        from switchboard.server.handlers.git_tools import _handle_git_push

        self.mock_db.get_task = AsyncMock(return_value=None)

        result = await _handle_git_push({"task_id": "proj/unknown"})

        assert result["pushed"] is False
        assert result["error"] == "not_found"

    async def test_push_no_pat(self):
        """git_push returns error when no PAT is configured."""
        from switchboard.server.handlers.git_tools import _handle_git_push

        self.mock_pat.side_effect = ValueError("No PAT configured")
        self.mock_run.side_effect = [
            (b"my-branch\n", b"", 0),    # rev-parse
            (b"abc123 fix\n", b"", 0),   # log
        ]

        result = await _handle_git_push({"task_id": "proj/task1"})

        assert result["pushed"] is False
        assert result["error"] == "no_pat"

    async def test_push_when_remote_branch_missing(self):
        """git_push pushes even when origin/branch doesn't exist yet."""
        from switchboard.server.handlers.git_tools import _handle_git_push

        self.mock_run.side_effect = [
            (b"my-branch\n", b"", 0),            # rev-parse
            (b"", b"unknown revision", 128),     # log fails — no origin/branch
            (b"abc123 init\n", b"", 0),          # log -1 shows commits exist
            (b"", b"", 0),                       # push succeeds
            (b"", b"", 0),                       # fetch origin
        ]

        result = await _handle_git_push({"task_id": "proj/task1"})

        assert result["pushed"] is True
        assert result["commits"] == 1


# ---------------------------------------------------------------------------
# git_fetch tests
# ---------------------------------------------------------------------------


class TestGitFetch:
    """Tests for the git_fetch MCP tool handler."""

    @pytest.fixture(autouse=True)
    def _patches(self, tmp_path):
        self.worktree = str(tmp_path / "wt")
        os.makedirs(self.worktree)
        self.bare_path = str(tmp_path / ".bare")
        os.makedirs(self.bare_path)
        self.mock_run = AsyncMock(return_value=(b"", b"", 0))
        self.mock_pat = AsyncMock(return_value="ghp_testtoken123")
        self.task = {
            "id": "proj/task1",
            "project_id": "proj",
            "branch": "my-branch",
            "worktree_path": self.worktree,
        }
        self.project = {
            "id": "proj",
            "repo": "https://github.com/org/repo.git",
            "working_dir": str(tmp_path),
        }
        self.patches = [
            patch("switchboard.server.handlers.git_tools._run_as_worker", self.mock_run),
            patch("switchboard.server.handlers.git_tools.get_github_pat", self.mock_pat),
            patch("switchboard.server.handlers.git_tools.db"),
        ]
        mocks = [p.start() for p in self.patches]
        self.mock_db = mocks[2]
        self.mock_db.get_task = AsyncMock(return_value=self.task)
        self.mock_db.get_project = AsyncMock(return_value=self.project)
        yield
        for p in self.patches:
            p.stop()

    async def test_fetch_specific_ref(self):
        """git_fetch with ref fetches specific branch."""
        from switchboard.server.handlers.git_tools import _handle_git_fetch

        result = await _handle_git_fetch({"task_id": "proj/task1", "ref": "main"})

        assert result["fetched"] is True
        assert result["ref"] == "main"

        # Should have called fetch twice: bare repo + worktree
        assert self.mock_run.await_count == 2
        # First call: bare repo fetch with auth URL and specific refspec
        bare_call = self.mock_run.call_args_list[0]
        assert "+refs/heads/main:refs/remotes/origin/main" in bare_call[0]

    async def test_fetch_all(self):
        """git_fetch without ref fetches all branches."""
        from switchboard.server.handlers.git_tools import _handle_git_fetch

        result = await _handle_git_fetch({"task_id": "proj/task1"})

        assert result["fetched"] is True
        assert result["ref"] == "all"

        # Two calls: bare repo + worktree
        assert self.mock_run.await_count == 2
        # First call: bare repo fetch all
        bare_call = self.mock_run.call_args_list[0]
        assert "+refs/heads/*:refs/remotes/origin/*" in bare_call[0]

    async def test_fetch_task_not_found(self):
        """git_fetch returns error for unknown task."""
        from switchboard.server.handlers.git_tools import _handle_git_fetch

        self.mock_db.get_task = AsyncMock(return_value=None)

        result = await _handle_git_fetch({"task_id": "proj/unknown"})

        assert result["fetched"] is False
        assert result["error"] == "not_found"

    async def test_fetch_no_pat(self):
        """git_fetch returns error when no PAT is configured."""
        from switchboard.server.handlers.git_tools import _handle_git_fetch

        self.mock_pat.side_effect = ValueError("No PAT configured")

        result = await _handle_git_fetch({"task_id": "proj/task1"})

        assert result["fetched"] is False
        assert result["error"] == "no_pat"

    async def test_fetch_failure(self):
        """git_fetch returns error when fetch command fails."""
        from switchboard.server.handlers.git_tools import _handle_git_fetch

        self.mock_run.return_value = (b"", b"fatal: could not read\n", 128)

        result = await _handle_git_fetch({"task_id": "proj/task1", "ref": "main"})

        assert result["fetched"] is False
        assert result["error"] == "fetch_failed"


# ---------------------------------------------------------------------------
# Hook script tests
# ---------------------------------------------------------------------------


class TestHookScripts:
    """Tests for the block-git-push.sh and block-git-fetch.sh hook scripts."""

    def test_block_git_push_script_output(self):
        """block-git-push.sh outputs valid JSON with deny decision."""
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "hooks", "block-git-push.sh",
        )
        result = subprocess.run(
            ["bash", script_path],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        output = data["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "git_push" in output["permissionDecisionReason"]

    def test_block_git_fetch_script_output(self):
        """block-git-fetch.sh outputs valid JSON with deny decision."""
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "hooks", "block-git-fetch.sh",
        )
        result = subprocess.run(
            ["bash", script_path],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        output = data["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny"
        assert "git_fetch" in output["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# Tool registration tests
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """Tests for git_push/git_fetch tool registration on worker endpoint."""

    def test_tools_in_worker_allowlist(self):
        """git_push and git_fetch are in WORKER_TOOL_ALLOWLIST."""
        from switchboard.server.tools import WORKER_TOOL_ALLOWLIST
        assert "git_push" in WORKER_TOOL_ALLOWLIST
        assert "git_fetch" in WORKER_TOOL_ALLOWLIST

    def test_tools_in_worker_tools(self):
        """git_push and git_fetch are in WORKER_TOOLS."""
        from switchboard.server.tools import WORKER_TOOLS
        names = {t.name for t in WORKER_TOOLS}
        assert "git_push" in names
        assert "git_fetch" in names

    def test_handlers_registered(self):
        """git_push and git_fetch handlers are registered in TOOL_HANDLERS."""
        from switchboard.server.dispatch import TOOL_HANDLERS
        assert "git_push" in TOOL_HANDLERS
        assert "git_fetch" in TOOL_HANDLERS
