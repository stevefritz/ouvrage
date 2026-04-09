"""Tests for provider-based git operations.

Verifies that push, fetch, PR creation, and status tracking go through the
provider interface rather than hardcoded GitHub API calls.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from switchboard.git.providers.base import RepoInfo, PRResult


def _make_mock_provider(auth_url="https://oauth2:tok@github.com/org/repo.git"):
    """Create a mock GitProvider with default implementations."""
    provider = MagicMock()
    provider.build_authenticated_url = MagicMock(return_value=auth_url)
    provider.parse_repo_url = MagicMock(
        return_value=RepoInfo(owner="org", repo="repo", hostname="github.com")
    )
    provider.create_pr = AsyncMock(return_value=PRResult(url="https://github.com/org/repo/pull/1", number=1))
    provider.get_pr_status = AsyncMock(return_value={"state": "open", "merged": False})
    provider.parse_pr_url = MagicMock(
        return_value=(RepoInfo(owner="org", repo="repo", hostname="github.com"), 1)
    )
    return provider


# ---------------------------------------------------------------------------
# _resolve_push_url uses provider interface
# ---------------------------------------------------------------------------

class TestResolvePushUrl:
    """_resolve_push_url goes through provider.build_authenticated_url."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mock_provider = _make_mock_provider()
        self.project = {
            "id": "proj",
            "repo": "https://github.com/org/repo.git",
            "working_dir": "/work/proj",
            "default_branch": "main",
        }


    async def test_raises_if_project_not_found(self):
        """_resolve_push_url raises ValueError when project doesn't exist."""
        from switchboard.git.operations import _resolve_push_url

        with patch("switchboard.git.operations.db.get_project", AsyncMock(return_value=None)):
            with pytest.raises(ValueError, match="not found"):
                await _resolve_push_url("nonexistent")


# ---------------------------------------------------------------------------
# _ensure_branch_pushed uses provider interface (via _resolve_push_url)
# ---------------------------------------------------------------------------

class TestEnsureBranchPushed:
    """_ensure_branch_pushed uses provider-based URL for push."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.worktree = str(tmp_path / "wt")
        os.makedirs(self.worktree)
        self.mock_provider = _make_mock_provider(
            "https://oauth2:tok@github.com/org/repo.git"
        )
        self.project = {
            "id": "proj",
            "repo": "https://github.com/org/repo.git",
            "working_dir": str(tmp_path),
            "default_branch": "main",
        }
        self.task = {
            "id": "proj/t1",
            "project_id": "proj",
            "branch": "feature",
            "worktree_path": self.worktree,
        }

    async def test_uses_provider_url_for_push(self):
        """_ensure_branch_pushed pushes via provider.build_authenticated_url output."""
        from switchboard.git.operations import _ensure_branch_pushed

        mock_run = AsyncMock(side_effect=[
            (b"abc123 refs/heads/feature\n", b"", 0),  # ls-remote — branch exists on remote
            (b"abc fix\n", b"", 0),  # log origin/branch..HEAD — unpushed commits
            (b"", b"", 0),  # push
        ])
        mock_resolve = AsyncMock(return_value=(self.mock_provider, "tok123"))

        with patch("switchboard.git.operations._run_as_worker", mock_run):
            with patch("switchboard.git.operations.resolve_credential", mock_resolve):
                with patch("switchboard.git.operations.db.get_project",
                           AsyncMock(return_value=self.project)):
                    with patch("switchboard.git.operations.db.post_task_message", AsyncMock()):
                        result = await _ensure_branch_pushed("proj/t1", self.task)

        assert result is True
        # Verify push used the authenticated URL from provider
        push_call = mock_run.call_args_list[2]
        push_args = push_call[0]
        assert any("oauth2:tok@github.com" in str(a) for a in push_args)

    async def test_fails_gracefully_when_no_credential(self):
        """_ensure_branch_pushed returns False when credential resolution fails."""
        from switchboard.git.operations import _ensure_branch_pushed

        mock_resolve = AsyncMock(side_effect=ValueError("No credential"))
        with patch("switchboard.git.operations.resolve_credential", mock_resolve):
            with patch("switchboard.git.operations.db.get_project",
                       AsyncMock(return_value=self.project)):
                with patch("switchboard.git.operations.db.post_task_message", AsyncMock()):
                    result = await _ensure_branch_pushed("proj/t1", self.task)

        assert result is False


# ---------------------------------------------------------------------------
# _maybe_create_pr calls provider.create_pr()
# ---------------------------------------------------------------------------

class TestMaybeCreatePr:
    """_maybe_create_pr uses provider.create_pr() instead of direct GitHub API."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.worktree = str(tmp_path / "wt")
        os.makedirs(self.worktree)
        self.mock_provider = _make_mock_provider()
        self.task = {
            "id": "proj/t1",
            "project_id": "proj",
            "goal": "Add widget sorting",
            "branch": "add-sorting",
            "worktree_path": self.worktree,
            "auto_pr": True,
            "base_branch": "main",
            "component_id": None,
            "parent_task_id": None,
        }
        self.project = {
            "id": "proj",
            "repo": "https://github.com/org/repo.git",
            "working_dir": str(tmp_path),
            "default_branch": "main",
        }


    async def test_skips_when_no_credential(self):
        """_maybe_create_pr posts error message when credential resolution fails."""
        from switchboard.git.operations import _maybe_create_pr

        mock_resolve = AsyncMock(side_effect=ValueError("No credential configured"))
        mock_post = AsyncMock()

        with patch("switchboard.git.operations.db.get_task", AsyncMock(return_value=self.task)):
            with patch("switchboard.git.operations.db.get_project", AsyncMock(return_value=self.project)):
                with patch("switchboard.git.operations.db.get_dependents", AsyncMock(return_value=[])):
                    with patch("switchboard.git.operations.resolve_credential", mock_resolve):
                        with patch("switchboard.git.operations.db.post_task_message", mock_post):
                            await _maybe_create_pr("proj/t1")

        # Should post an error message instead of calling create_pr
        self.mock_provider.create_pr.assert_not_called()
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert "credential" in call_kwargs["title"].lower() or "credential" in call_kwargs["content"].lower()


# ---------------------------------------------------------------------------
# _check_pr_status uses provider.get_pr_status()
# ---------------------------------------------------------------------------

class TestCheckPrStatusProvider:
    """_check_pr_status uses provider.get_pr_status(), not direct httpx."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mock_provider = _make_mock_provider()
        self.project = {
            "id": "proj",
            "repo": "https://github.com/org/repo.git",
        }

    async def test_calls_provider_get_pr_status(self):
        """_check_pr_status delegates to provider.get_pr_status."""
        from switchboard.dispatch.pr_sweep import _check_pr_status

        mock_resolve = AsyncMock(return_value=(self.mock_provider, "tok123"))
        with patch("switchboard.dispatch.pr_sweep.resolve_credential", mock_resolve):
            with patch("switchboard.dispatch.pr_sweep.db.get_project",
                       AsyncMock(return_value=self.project)):
                status = await _check_pr_status(
                    "https://github.com/org/repo/pull/1", "proj"
                )

        self.mock_provider.get_pr_status.assert_called_once()
        assert status == "open"


# ---------------------------------------------------------------------------
# git_push and git_fetch use resolve_credential
# ---------------------------------------------------------------------------

class TestGitToolsUseProviderInterface:
    """git_push and git_fetch go through resolve_credential."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.worktree = str(tmp_path / "wt")
        os.makedirs(self.worktree)
        self.mock_provider = _make_mock_provider(
            "https://oauth2:tok@github.com/org/repo.git"
        )
        self.task = {
            "id": "proj/t1",
            "project_id": "proj",
            "branch": "my-branch",
            "worktree_path": self.worktree,
        }
        self.project = {
            "id": "proj",
            "repo": "https://github.com/org/repo.git",
            "working_dir": str(tmp_path),
        }
        self.mock_run = AsyncMock(return_value=(b"", b"", 0))
        self.mock_resolve = AsyncMock(return_value=(self.mock_provider, "tok123"))
        self.patches = [
            patch("switchboard.server.handlers.git_tools._run_as_worker", self.mock_run),
            patch("switchboard.server.handlers.git_tools.resolve_credential", self.mock_resolve),
            patch("switchboard.server.handlers.git_tools.db"),
        ]
        started = [p.start() for p in self.patches]
        self.mock_db = started[2]
        self.mock_db.get_task = AsyncMock(return_value=self.task)
        self.mock_db.get_project = AsyncMock(return_value=self.project)

    def teardown_method(self):
        for p in self.patches:
            try:
                p.stop()
            except RuntimeError:
                pass


# ---------------------------------------------------------------------------
# GitHubProvider.parse_pr_url
# ---------------------------------------------------------------------------

class TestGitHubProviderParsePrUrl:
    """GitHubProvider.parse_pr_url correctly parses GitHub PR URLs."""

    def setup_method(self):
        from switchboard.git.providers.github import GitHubProvider
        self.provider = GitHubProvider()


    def test_http_scheme(self):
        info, number = self.provider.parse_pr_url("http://github.com/acme/widgets/pull/7")
        assert number == 7


    def test_empty_raises(self):
        with pytest.raises(ValueError):
            self.provider.parse_pr_url("")
