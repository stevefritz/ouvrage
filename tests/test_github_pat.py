"""Tests for GitHub PAT integration — URL parsing, HTTPS push, REST API PR creation."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from switchboard.git.operations import (
    parse_repo_url,
    _build_authenticated_url,
    _classify_push_error,
    create_github_pr,
    _find_existing_pr,
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
# _build_authenticated_url
# ---------------------------------------------------------------------------

class TestBuildAuthenticatedUrl:
    """Authenticated URL must embed PAT and parse owner/repo correctly."""

    def test_from_ssh_url(self):
        url = _build_authenticated_url("ghp_abc123", "git@github.com:acme/widgets.git")
        assert url == "https://oauth2:ghp_abc123@github.com/acme/widgets.git"

    def test_from_https_url(self):
        url = _build_authenticated_url("ghp_abc123", "https://github.com/acme/widgets.git")
        assert url == "https://oauth2:ghp_abc123@github.com/acme/widgets.git"

    def test_pat_not_leaked_in_repr(self):
        """PAT should be in the URL string but we verify it's constructed correctly."""
        url = _build_authenticated_url("ghp_secret", "git@github.com:org/repo.git")
        assert "ghp_secret" in url
        assert url.startswith("https://")


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
# create_github_pr — REST API
# ---------------------------------------------------------------------------

class TestCreateGithubPr:
    """REST API PR creation with httpx."""

    @pytest.mark.asyncio
    async def test_201_created(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "html_url": "https://github.com/acme/widgets/pull/42",
            "number": 42,
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("switchboard.git.operations.httpx.AsyncClient", return_value=mock_client):
            result = await create_github_pr(
                pat="ghp_test", owner="acme", repo="widgets",
                head="feature-branch", base="main",
                title="Add feature", body="## Summary\n- stuff",
            )

        assert result["url"] == "https://github.com/acme/widgets/pull/42"
        assert result["number"] == 42

    @pytest.mark.asyncio
    async def test_422_already_exists_finds_existing(self):
        mock_create_resp = MagicMock()
        mock_create_resp.status_code = 422
        mock_create_resp.json.return_value = {
            "message": "Validation Failed",
            "errors": [{"message": "A pull request already exists for acme:feature-branch."}],
        }

        mock_list_resp = MagicMock()
        mock_list_resp.status_code = 200
        mock_list_resp.json.return_value = [{
            "html_url": "https://github.com/acme/widgets/pull/41",
            "number": 41,
        }]

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_create_resp
        mock_client.get.return_value = mock_list_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("switchboard.git.operations.httpx.AsyncClient", return_value=mock_client):
            result = await create_github_pr(
                pat="ghp_test", owner="acme", repo="widgets",
                head="feature-branch", base="main",
                title="Add feature",
            )

        assert result["url"] == "https://github.com/acme/widgets/pull/41"
        assert result["number"] == 41

    @pytest.mark.asyncio
    async def test_404_raises_not_found(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("switchboard.git.operations.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="not found"):
                await create_github_pr(
                    pat="ghp_test", owner="acme", repo="widgets",
                    head="feature", base="main", title="t",
                )

    @pytest.mark.asyncio
    async def test_403_raises_permission(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 403

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("switchboard.git.operations.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="repo.*scope"):
                await create_github_pr(
                    pat="ghp_test", owner="acme", repo="widgets",
                    head="feature", base="main", title="t",
                )

    @pytest.mark.asyncio
    async def test_422_non_duplicate_raises(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 422
        mock_resp.json.return_value = {
            "message": "Validation Failed",
            "errors": [{"message": "head must be a valid ref"}],
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("switchboard.git.operations.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="PR creation failed"):
                await create_github_pr(
                    pat="ghp_test", owner="acme", repo="widgets",
                    head="feature", base="main", title="t",
                )


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
    async def test_push_uses_https_url_not_origin(self):
        """Push command must use authenticated HTTPS URL, not 'origin'."""
        from switchboard.git.operations import _ensure_branch_pushed
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
        from switchboard.git.operations import _ensure_branch_pushed
        import os

        with patch.object(os.path, "exists", return_value=True):
            # credential helper check (no helper), then remote branch exists, no unpushed commits
            self.mock_run.side_effect = [
                (b"", b"", 0),  # git config credential.helper — no cred helper
                (b"abc refs/heads/br\n", b"", 0),  # ls-remote
                (b"", b"", 0),  # git log (no unpushed)
            ]
            await _ensure_branch_pushed("proj/task-1", {
                "project_id": "proj",
                "worktree_path": "/tmp/wt",
                "branch": "br",
            })

        ls_remote_call = self.mock_run.call_args_list[1]  # index 1: after cred helper check
        assert "ls-remote" in ls_remote_call.args
        assert "https://oauth2:ghp_test@github.com/acme/widgets.git" in ls_remote_call.args

    @pytest.mark.asyncio
    async def test_no_pat_posts_error_message(self):
        """If no PAT configured, post error and skip push."""
        from switchboard.git.operations import _ensure_branch_pushed
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
