"""Tests for provider-based git operations.

Verifies that push, fetch, PR creation, and status tracking go through the
provider interface rather than hardcoded GitHub API calls.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouvrage.git.providers.base import RepoInfo, PRResult


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

    async def test_calls_resolve_credential(self):
        """_resolve_push_url calls resolve_credential, not get_github_pat."""
        from ouvrage.git.operations import _resolve_push_url

        mock_resolve = AsyncMock(return_value=(self.mock_provider, "tok123"))
        with patch("ouvrage.git.operations.resolve_credential", mock_resolve):
            with patch("ouvrage.git.operations.db.get_project",
                       AsyncMock(return_value=self.project)):
                url = await _resolve_push_url("proj")

        mock_resolve.assert_called_once_with(self.project)
        assert url == self.mock_provider.build_authenticated_url.return_value

    async def test_calls_provider_build_authenticated_url(self):
        """_resolve_push_url calls provider.build_authenticated_url with repo URL."""
        from ouvrage.git.operations import _resolve_push_url

        mock_resolve = AsyncMock(return_value=(self.mock_provider, "tok123"))
        with patch("ouvrage.git.operations.resolve_credential", mock_resolve):
            with patch("ouvrage.git.operations.db.get_project",
                       AsyncMock(return_value=self.project)):
                await _resolve_push_url("proj")

        self.mock_provider.build_authenticated_url.assert_called_once_with(
            self.project["repo"], "tok123"
        )

    async def test_raises_if_no_credential(self):
        """_resolve_push_url raises ValueError when resolve_credential fails."""
        from ouvrage.git.operations import _resolve_push_url

        mock_resolve = AsyncMock(side_effect=ValueError("No credential configured"))
        with patch("ouvrage.git.operations.resolve_credential", mock_resolve):
            with patch("ouvrage.git.operations.db.get_project",
                       AsyncMock(return_value=self.project)):
                with pytest.raises(ValueError, match="No credential"):
                    await _resolve_push_url("proj")

    async def test_raises_if_project_not_found(self):
        """_resolve_push_url raises ValueError when project doesn't exist."""
        from ouvrage.git.operations import _resolve_push_url

        with patch("ouvrage.git.operations.db.get_project", AsyncMock(return_value=None)):
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
        from ouvrage.git.operations import _ensure_branch_pushed

        mock_run = AsyncMock(side_effect=[
            (b"abc123 refs/heads/feature\n", b"", 0),  # ls-remote — branch exists on remote
            (b"abc fix\n", b"", 0),  # log origin/branch..HEAD — unpushed commits
            (b"", b"", 0),  # push
        ])
        mock_resolve = AsyncMock(return_value=(self.mock_provider, "tok123"))

        with patch("ouvrage.git.operations._run_as_worker", mock_run):
            with patch("ouvrage.git.operations.resolve_credential", mock_resolve):
                with patch("ouvrage.git.operations.db.get_project",
                           AsyncMock(return_value=self.project)):
                    with patch("ouvrage.git.operations.db.post_task_message", AsyncMock()):
                        result = await _ensure_branch_pushed("proj/t1", self.task)

        assert result is True
        # Verify push used the authenticated URL from provider
        push_call = mock_run.call_args_list[2]
        push_args = push_call[0]
        assert any("oauth2:tok@github.com" in str(a) for a in push_args)

    async def test_fails_gracefully_when_no_credential(self):
        """_ensure_branch_pushed returns False when credential resolution fails."""
        from ouvrage.git.operations import _ensure_branch_pushed

        mock_resolve = AsyncMock(side_effect=ValueError("No credential"))
        with patch("ouvrage.git.operations.resolve_credential", mock_resolve):
            with patch("ouvrage.git.operations.db.get_project",
                       AsyncMock(return_value=self.project)):
                with patch("ouvrage.git.operations.db.post_task_message", AsyncMock()):
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

    async def test_calls_provider_create_pr(self):
        """_maybe_create_pr calls provider.create_pr with correct args."""
        from ouvrage.git.operations import _maybe_create_pr

        mock_resolve = AsyncMock(return_value=(self.mock_provider, "tok123"))

        with patch("ouvrage.git.operations.db.get_task", AsyncMock(return_value=self.task)):
            with patch("ouvrage.git.operations.db.get_project", AsyncMock(return_value=self.project)):
                with patch("ouvrage.git.operations.db.get_dependents", AsyncMock(return_value=[])):
                    with patch("ouvrage.git.operations.db.get_chain",
                               AsyncMock(return_value=[self.task])):
                        with patch("ouvrage.git.operations.resolve_credential", mock_resolve):
                            with patch("ouvrage.git.operations.db.add_artifact", AsyncMock()):
                                with patch("ouvrage.git.operations.db.post_task_message", AsyncMock()):
                                    await _maybe_create_pr("proj/t1")

        self.mock_provider.create_pr.assert_called_once()
        call_kwargs = self.mock_provider.create_pr.call_args[1]
        assert call_kwargs["credential"] == "tok123"
        assert call_kwargs["head"] == "add-sorting"
        assert call_kwargs["base"] == "main"
        assert "Add widget sorting" in call_kwargs["title"]

    async def test_skips_when_no_credential(self):
        """_maybe_create_pr posts error message when credential resolution fails."""
        from ouvrage.git.operations import _maybe_create_pr

        mock_resolve = AsyncMock(side_effect=ValueError("No credential configured"))
        mock_post = AsyncMock()

        with patch("ouvrage.git.operations.db.get_task", AsyncMock(return_value=self.task)):
            with patch("ouvrage.git.operations.db.get_project", AsyncMock(return_value=self.project)):
                with patch("ouvrage.git.operations.db.get_dependents", AsyncMock(return_value=[])):
                    with patch("ouvrage.git.operations.resolve_credential", mock_resolve):
                        with patch("ouvrage.git.operations.db.post_task_message", mock_post):
                            await _maybe_create_pr("proj/t1")

        # Should post an error message instead of calling create_pr
        self.mock_provider.create_pr.assert_not_called()
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args[1]
        assert "credential" in call_kwargs["title"].lower() or "credential" in call_kwargs["content"].lower()

    async def test_pr_url_from_provider_stored(self):
        """_maybe_create_pr stores the URL returned by provider.create_pr."""
        from ouvrage.git.operations import _maybe_create_pr

        expected_url = "https://github.com/org/repo/pull/42"
        self.mock_provider.create_pr = AsyncMock(
            return_value=PRResult(url=expected_url, number=42)
        )
        mock_resolve = AsyncMock(return_value=(self.mock_provider, "tok123"))
        mock_add_artifact = AsyncMock()

        with patch("ouvrage.git.operations.db.get_task", AsyncMock(return_value=self.task)):
            with patch("ouvrage.git.operations.db.get_project", AsyncMock(return_value=self.project)):
                with patch("ouvrage.git.operations.db.get_dependents", AsyncMock(return_value=[])):
                    with patch("ouvrage.git.operations.db.get_chain",
                               AsyncMock(return_value=[self.task])):
                        with patch("ouvrage.git.operations.resolve_credential", mock_resolve):
                            with patch("ouvrage.git.operations.db.add_artifact", mock_add_artifact):
                                with patch("ouvrage.git.operations.db.post_task_message", AsyncMock()):
                                    await _maybe_create_pr("proj/t1")

        mock_add_artifact.assert_called_once_with("proj/t1", "pr_url", expected_url)


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
        from ouvrage.dispatch.pr_sweep import _check_pr_status

        mock_resolve = AsyncMock(return_value=(self.mock_provider, "tok123"))
        with patch("ouvrage.dispatch.pr_sweep.resolve_credential", mock_resolve):
            with patch("ouvrage.dispatch.pr_sweep.db.get_project",
                       AsyncMock(return_value=self.project)):
                status = await _check_pr_status(
                    "https://github.com/org/repo/pull/1", "proj"
                )

        self.mock_provider.get_pr_status.assert_called_once()
        assert status == "open"

    async def test_returns_merged_when_provider_says_merged(self):
        """Status 'merged' is returned when provider reports merged=True."""
        from ouvrage.dispatch.pr_sweep import _check_pr_status

        self.mock_provider.get_pr_status = AsyncMock(
            return_value={"state": "closed", "merged": True}
        )
        mock_resolve = AsyncMock(return_value=(self.mock_provider, "tok123"))
        with patch("ouvrage.dispatch.pr_sweep.resolve_credential", mock_resolve):
            with patch("ouvrage.dispatch.pr_sweep.db.get_project",
                       AsyncMock(return_value=self.project)):
                status = await _check_pr_status(
                    "https://github.com/org/repo/pull/1", "proj"
                )

        assert status == "merged"

    async def test_returns_closed_when_provider_says_closed(self):
        """Status 'closed' when provider says state=closed and merged=False."""
        from ouvrage.dispatch.pr_sweep import _check_pr_status

        self.mock_provider.get_pr_status = AsyncMock(
            return_value={"state": "closed", "merged": False}
        )
        mock_resolve = AsyncMock(return_value=(self.mock_provider, "tok123"))
        with patch("ouvrage.dispatch.pr_sweep.resolve_credential", mock_resolve):
            with patch("ouvrage.dispatch.pr_sweep.db.get_project",
                       AsyncMock(return_value=self.project)):
                status = await _check_pr_status(
                    "https://github.com/org/repo/pull/1", "proj"
                )

        assert status == "closed"

    async def test_uses_provider_parse_pr_url(self):
        """_check_pr_status uses provider.parse_pr_url to parse the PR URL."""
        from ouvrage.dispatch.pr_sweep import _check_pr_status

        mock_resolve = AsyncMock(return_value=(self.mock_provider, "tok123"))
        with patch("ouvrage.dispatch.pr_sweep.resolve_credential", mock_resolve):
            with patch("ouvrage.dispatch.pr_sweep.db.get_project",
                       AsyncMock(return_value=self.project)):
                await _check_pr_status(
                    "https://github.com/org/repo/pull/99", "proj"
                )

        self.mock_provider.parse_pr_url.assert_called_once_with(
            "https://github.com/org/repo/pull/99"
        )


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
            patch("ouvrage.server.handlers.git_tools._run_as_worker", self.mock_run),
            patch("ouvrage.server.handlers.git_tools.resolve_credential", self.mock_resolve),
            patch("ouvrage.server.handlers.git_tools.db"),
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

    async def test_git_push_calls_resolve_credential(self):
        """git_push calls resolve_credential instead of get_github_pat."""
        from ouvrage.server.handlers.git_tools import _handle_git_push

        self.mock_run.side_effect = [
            (b"my-branch\n", b"", 0),   # rev-parse HEAD
            (b"abc fix\n", b"", 0),     # log origin/branch..HEAD
            (b"", b"", 0),              # push
            (b"abc123\n", b"", 0),      # rev-parse HEAD for tracking ref
            (b"", b"", 0),              # update-ref
        ]

        await _handle_git_push({"task_id": "proj/t1"})

        self.mock_resolve.assert_called_once_with(self.project)

    async def test_git_push_uses_provider_build_authenticated_url(self):
        """git_push uses the URL from provider.build_authenticated_url."""
        from ouvrage.server.handlers.git_tools import _handle_git_push

        self.mock_run.side_effect = [
            (b"my-branch\n", b"", 0),   # rev-parse HEAD
            (b"abc fix\n", b"", 0),     # log origin/branch..HEAD
            (b"", b"", 0),              # push
            (b"abc123\n", b"", 0),      # rev-parse HEAD
            (b"", b"", 0),              # update-ref
        ]

        result = await _handle_git_push({"task_id": "proj/t1"})

        assert result["pushed"] is True
        self.mock_provider.build_authenticated_url.assert_called_once_with(
            self.project["repo"], "tok123"
        )

    async def test_git_fetch_calls_resolve_credential(self):
        """git_fetch calls resolve_credential instead of get_github_pat."""
        from ouvrage.server.handlers.git_tools import _handle_git_fetch

        result = await _handle_git_fetch({"task_id": "proj/t1"})

        assert result["fetched"] is True
        self.mock_resolve.assert_called_once_with(self.project)

    async def test_git_fetch_uses_provider_build_authenticated_url(self):
        """git_fetch uses the URL from provider.build_authenticated_url."""
        from ouvrage.server.handlers.git_tools import _handle_git_fetch

        result = await _handle_git_fetch({"task_id": "proj/t1", "ref": "main"})

        assert result["fetched"] is True
        self.mock_provider.build_authenticated_url.assert_called_once_with(
            self.project["repo"], "tok123"
        )


# ---------------------------------------------------------------------------
# GitHubProvider.parse_pr_url
# ---------------------------------------------------------------------------

class TestGitHubProviderParsePrUrl:
    """GitHubProvider.parse_pr_url correctly parses GitHub PR URLs."""

    def setup_method(self):
        from ouvrage.git.providers.github import GitHubProvider
        self.provider = GitHubProvider()

    def test_standard_url(self):
        info, number = self.provider.parse_pr_url("https://github.com/acme/widgets/pull/42")
        assert info.owner == "acme"
        assert info.repo == "widgets"
        assert info.hostname == "github.com"
        assert number == 42

    def test_http_scheme(self):
        info, number = self.provider.parse_pr_url("http://github.com/acme/widgets/pull/7")
        assert number == 7

    def test_hyphenated_names(self):
        info, number = self.provider.parse_pr_url("https://github.com/my-org/my-repo/pull/100")
        assert info.owner == "my-org"
        assert info.repo == "my-repo"
        assert number == 100

    def test_trailing_whitespace_stripped(self):
        info, number = self.provider.parse_pr_url("  https://github.com/acme/widgets/pull/5  ")
        assert number == 5

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError):
            self.provider.parse_pr_url("https://gitlab.com/acme/widgets/pull/1")

    def test_missing_pull_raises(self):
        with pytest.raises(ValueError):
            self.provider.parse_pr_url("https://github.com/acme/widgets")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            self.provider.parse_pr_url("")
