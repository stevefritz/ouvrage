"""Tests for GitHub PAT integration — URL parsing, HTTPS push, push error classification."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from switchboard.git.operations import (
    parse_repo_url,
    _classify_push_error,
)


# ---------------------------------------------------------------------------
# parse_repo_url
# ---------------------------------------------------------------------------

class TestParseRepoUrl:
    """parse_repo_url must handle SSH and HTTPS formats."""


    def test_https_http_scheme(self):
        assert parse_repo_url("http://github.com/acme/widgets") == ("acme", "widgets")

    def test_owner_with_hyphens(self):
        assert parse_repo_url("git@github.com:my-org/my-repo.git") == ("my-org", "my-repo")


    def test_gitlab_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_repo_url("git@gitlab.com:acme/widgets.git")


# ---------------------------------------------------------------------------
# _classify_push_error
# ---------------------------------------------------------------------------

class TestClassifyPushError:
    """Push error classification for user-friendly messages."""

    def test_auth_failed(self):
        msg = _classify_push_error("fatal: Authentication failed for 'https://github.com/...'")
        assert "invalid or expired" in msg

    def test_permission_denied(self):
        msg = _classify_push_error("remote: Permission denied to oauth2")
        assert "repo" in msg.lower() and "scope" in msg.lower()

    def test_repo_not_found(self):
        msg = _classify_push_error("fatal: repository 'https://github.com/x/y.git/' not found")
        assert "not found" in msg.lower()

    def test_network_error(self):
        msg = _classify_push_error("fatal: unable to access 'https://...'")
        assert "reach GitHub" in msg


# ---------------------------------------------------------------------------
# _ensure_branch_pushed — PAT injection
# ---------------------------------------------------------------------------

class TestEnsureBranchPushedPat:
    """_ensure_branch_pushed must use authenticated HTTPS URL."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_run = AsyncMock(return_value=(b"abc123 refs/heads/test-branch\n", b"", 0))
        self.mock_resolve_url = AsyncMock(return_value="https://oauth2:ghp_test@github.com/acme/widgets.git")

        patches = [
            patch("switchboard.git.operations._run_as_worker", self.mock_run),
            patch("switchboard.git.operations._resolve_push_url", self.mock_resolve_url),
            patch("switchboard.git.operations.db.post_task_message", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()


    @pytest.mark.asyncio
    async def test_ls_remote_uses_https_url(self):
        """ls-remote should also use authenticated URL."""
        from switchboard.git.operations import _ensure_branch_pushed
        import os

        with patch.object(os.path, "exists", return_value=True):
            # remote branch exists, no unpushed commits
            self.mock_run.side_effect = [
                (b"abc refs/heads/br\n", b"", 0),  # ls-remote
                (b"", b"", 0),  # git log (no unpushed)
            ]
            await _ensure_branch_pushed("proj/task-1", {
                "project_id": "proj",
                "worktree_path": "/tmp/wt",
                "branch": "br",
            })

        ls_remote_call = self.mock_run.call_args_list[0]  # first call is ls-remote
        assert "ls-remote" in ls_remote_call.args
        assert "https://oauth2:ghp_test@github.com/acme/widgets.git" in ls_remote_call.args

