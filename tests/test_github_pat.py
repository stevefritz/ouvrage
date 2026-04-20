"""Tests for GitHub PAT integration — URL parsing, HTTPS push, push error classification."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from ouvrage.git.operations import (
    parse_repo_url,
    _classify_push_error,
)


# ---------------------------------------------------------------------------
# parse_repo_url
# ---------------------------------------------------------------------------

class TestParseRepoUrl:
    """parse_repo_url must handle SSH and HTTPS formats."""

    def test_ssh_with_git_suffix(self):
        assert parse_repo_url("git@github.com:acme/widgets.git") == ("acme", "widgets")

    def test_ssh_without_git_suffix(self):
        assert parse_repo_url("git@github.com:acme/widgets") == ("acme", "widgets")

    def test_https_with_git_suffix(self):
        assert parse_repo_url("https://github.com/acme/widgets.git") == ("acme", "widgets")

    def test_https_without_git_suffix(self):
        assert parse_repo_url("https://github.com/acme/widgets") == ("acme", "widgets")

    def test_https_http_scheme(self):
        assert parse_repo_url("http://github.com/acme/widgets") == ("acme", "widgets")

    def test_owner_with_hyphens(self):
        assert parse_repo_url("git@github.com:my-org/my-repo.git") == ("my-org", "my-repo")

    def test_repo_with_dots(self):
        assert parse_repo_url("git@github.com:org/repo.name.git") == ("org", "repo.name")

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_repo_url("not-a-url")

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

    def test_unknown_error(self):
        assert _classify_push_error("some random error") is None


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
            patch("ouvrage.git.operations._run_as_worker", self.mock_run),
            patch("ouvrage.git.operations._resolve_push_url", self.mock_resolve_url),
            patch("ouvrage.git.operations.db.post_task_message", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    @pytest.mark.asyncio
    async def test_push_uses_https_url_not_origin(self):
        """Push command must use authenticated HTTPS URL, not 'origin'."""
        from ouvrage.git.operations import _ensure_branch_pushed
        import os

        with patch.object(os.path, "exists", return_value=True):
            # ls-remote returns empty (no remote branch yet) — triggers push
            self.mock_run.return_value = (b"", b"", 0)
            await _ensure_branch_pushed("proj/task-1", {
                "project_id": "proj",
                "worktree_path": "/tmp/wt",
                "branch": "test-branch",
            })

        # Find the push call
        push_calls = [c for c in self.mock_run.call_args_list if "push" in c.args]
        assert len(push_calls) == 1
        push_args = push_calls[0].args
        assert "https://oauth2:ghp_test@github.com/acme/widgets.git" in push_args
        assert "origin" not in push_args

    @pytest.mark.asyncio
    async def test_ls_remote_uses_https_url(self):
        """ls-remote should also use authenticated URL."""
        from ouvrage.git.operations import _ensure_branch_pushed
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

    @pytest.mark.asyncio
    async def test_no_pat_posts_error_message(self):
        """If no PAT configured, post error and skip push."""
        from ouvrage.git.operations import _ensure_branch_pushed
        import os

        self.mock_resolve_url.side_effect = ValueError("No GitHub PAT configured")

        with patch.object(os.path, "exists", return_value=True):
            await _ensure_branch_pushed("proj/task-1", {
                "project_id": "proj",
                "worktree_path": "/tmp/wt",
                "branch": "br",
            })

        # Should not have called any git commands
        self.mock_run.assert_not_awaited()
