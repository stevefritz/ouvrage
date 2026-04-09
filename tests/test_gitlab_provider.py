"""Tests for GitLabProvider — URL parsing, auth, MR creation, status, self-hosted."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(status_code: int, json_data: dict | list) -> MagicMock:
    """Create a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_data)
    resp.raise_for_status = MagicMock()
    return resp


def _make_async_client(responses: list) -> MagicMock:
    """Create a mock async httpx client that returns responses in order."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=responses)
    client.post = AsyncMock(side_effect=responses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ---------------------------------------------------------------------------
# parse_repo_url
# ---------------------------------------------------------------------------

class TestGitLabParseRepoUrl:
    def setup_method(self):
        from switchboard.git.providers.gitlab import GitLabProvider
        self.provider = GitLabProvider()


    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            self.provider.parse_repo_url("not-a-url")

    def test_invalid_single_segment_raises(self):
        """A path with only one segment (no namespace) is invalid."""
        with pytest.raises(ValueError):
            self.provider.parse_repo_url("https://gitlab.com/project-only")


# ---------------------------------------------------------------------------
# build_authenticated_url
# ---------------------------------------------------------------------------

class TestGitLabBuildAuthenticatedUrl:
    def setup_method(self):
        from switchboard.git.providers.gitlab import GitLabProvider
        self.provider = GitLabProvider()


    def test_https_nested_groups(self):
        url = self.provider.build_authenticated_url(
            "https://gitlab.com/group/subgroup/project.git", "mytoken"
        )
        assert url == "https://oauth2:mytoken@gitlab.com/group/subgroup/project.git"

    def test_ssh_input_becomes_https(self):
        url = self.provider.build_authenticated_url(
            "git@gitlab.com:acme/widgets.git", "mytoken"
        )
        assert url == "https://oauth2:mytoken@gitlab.com/acme/widgets.git"


# ---------------------------------------------------------------------------
# validate_access
# ---------------------------------------------------------------------------

class TestGitLabValidateAccess:
    def setup_method(self):
        from switchboard.git.providers.gitlab import GitLabProvider
        from switchboard.git.providers.base import RepoInfo
        self.provider = GitLabProvider()
        self.repo_info = RepoInfo(owner="acme", repo="widgets", hostname="gitlab.com")


    async def test_insufficient_access_level(self):
        """access_level < 30 (Guest/Reporter) is rejected."""
        project_resp = _make_response(200, {
            "permissions": {
                "project_access": {"access_level": 20},  # Reporter
                "group_access": None,
            }
        })

        client = _make_async_client([project_resp])
        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.validate_access("mytoken", self.repo_info)

        assert result.valid is False
        assert "access_level" in result.error

    async def test_repo_not_found(self):
        project_resp = _make_response(404, {"message": "Not found"})

        client = _make_async_client([project_resp])
        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.validate_access("mytoken", self.repo_info)

        assert result.valid is False
        assert "not found" in result.error.lower()


    async def test_forbidden_403(self):
        project_resp = _make_response(403, {"message": "Forbidden"})

        client = _make_async_client([project_resp])
        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.validate_access("badtoken", self.repo_info)

        assert result.valid is False

    async def test_null_permissions_still_valid(self):
        """Public repos may have null permissions — treat as valid (token introspection validates user)."""
        project_resp = _make_response(200, {"permissions": None})
        token_resp = _make_response(200, {"name": "Carol", "user_id": 10})

        client = _make_async_client([project_resp, token_resp])
        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.validate_access("mytoken", self.repo_info)

        assert result.valid is True


    async def test_network_error_returns_invalid(self):
        """Network exceptions return ValidationResult(valid=False)."""
        client = MagicMock()
        client.get = AsyncMock(side_effect=Exception("Connection refused"))
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.validate_access("mytoken", self.repo_info)

        assert result.valid is False
        assert "Connection refused" in result.error


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------

class TestGitLabCreatePr:
    def setup_method(self):
        from switchboard.git.providers.gitlab import GitLabProvider
        from switchboard.git.providers.base import RepoInfo
        self.provider = GitLabProvider()
        self.repo_info = RepoInfo(owner="acme", repo="widgets", hostname="gitlab.com")


    async def test_create_pr_already_exists_409(self):
        """409 with 'already exists' — finds existing MR."""
        conflict_resp = _make_response(409, {"message": "Another open merge request already exists"})
        list_resp = _make_response(200, [{
            "iid": 7,
            "web_url": "https://gitlab.com/acme/widgets/-/merge_requests/7",
        }])

        client = AsyncMock()
        client.post = AsyncMock(return_value=conflict_resp)
        client.get = AsyncMock(return_value=list_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.create_pr(
                "mytoken", self.repo_info,
                head="feat", base="main", title="Title",
            )

        assert result.number == 7
        assert result.url == "https://gitlab.com/acme/widgets/-/merge_requests/7"


    async def test_create_pr_repo_not_found(self):
        not_found = _make_response(404, {"message": "Not found"})

        client = AsyncMock()
        client.post = AsyncMock(return_value=not_found)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            with pytest.raises(ValueError, match="not found"):
                await self.provider.create_pr(
                    "mytoken", self.repo_info,
                    head="feat", base="main", title="Title",
                )

    async def test_create_pr_forbidden(self):
        forbidden = _make_response(403, {"message": "Forbidden"})

        client = AsyncMock()
        client.post = AsyncMock(return_value=forbidden)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            with pytest.raises(ValueError, match="permission"):
                await self.provider.create_pr(
                    "mytoken", self.repo_info,
                    head="feat", base="main", title="Title",
                )

    async def test_create_pr_nested_groups(self):
        """Nested group paths are URL-encoded correctly in API calls."""
        from switchboard.git.providers.base import RepoInfo
        nested_repo = RepoInfo(
            owner="group/subgroup", repo="project", hostname="gitlab.com"
        )
        mr_resp = _make_response(201, {
            "iid": 1,
            "web_url": "https://gitlab.com/group/subgroup/project/-/merge_requests/1",
        })

        captured_urls = []

        async def capturing_post(url, **kwargs):
            captured_urls.append(url)
            return mr_resp

        client = AsyncMock()
        client.post = AsyncMock(side_effect=capturing_post)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.create_pr(
                "mytoken", nested_repo,
                head="feat", base="main", title="Title",
            )

        assert result.number == 1
        # The path should be URL-encoded (/ → %2F)
        assert "%2F" in captured_urls[0]


# ---------------------------------------------------------------------------
# get_pr_status
# ---------------------------------------------------------------------------

class TestGitLabGetPrStatus:
    def setup_method(self):
        from switchboard.git.providers.gitlab import GitLabProvider
        from switchboard.git.providers.base import RepoInfo
        self.provider = GitLabProvider()
        self.repo_info = RepoInfo(owner="acme", repo="widgets", hostname="gitlab.com")

    async def test_opened_state(self):
        resp = _make_response(200, {"state": "opened", "iid": 1})

        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.get_pr_status("tok", self.repo_info, 1)

        assert result["state"] == "open"
        assert result["merged"] is False

    async def test_merged_state(self):
        resp = _make_response(200, {"state": "merged", "iid": 2})

        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.get_pr_status("tok", self.repo_info, 2)

        assert result["merged"] is True
        assert result["state"] == "merged"

    async def test_closed_state(self):
        resp = _make_response(200, {"state": "closed", "iid": 3})

        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.get_pr_status("tok", self.repo_info, 3)

        assert result["state"] == "closed"
        assert result["merged"] is False

    async def test_raises_on_http_error(self):
        resp = _make_response(404, {"message": "Not found"})
        resp.raise_for_status = MagicMock(side_effect=Exception("404 not found"))

        client = AsyncMock()
        client.get = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            with pytest.raises(Exception):
                await self.provider.get_pr_status("tok", self.repo_info, 99)


# ---------------------------------------------------------------------------
# parse_pr_url
# ---------------------------------------------------------------------------

class TestGitLabParsePrUrl:
    def setup_method(self):
        from switchboard.git.providers.gitlab import GitLabProvider
        self.provider = GitLabProvider()


    def test_deep_nested_groups(self):
        info, number = self.provider.parse_pr_url(
            "https://gitlab.com/a/b/c/project/-/merge_requests/1"
        )
        assert info.owner == "a/b/c"
        assert info.repo == "project"
        assert number == 1


    def test_empty_raises(self):
        with pytest.raises(ValueError):
            self.provider.parse_pr_url("")


# ---------------------------------------------------------------------------
# Provider name / hostname
# ---------------------------------------------------------------------------

class TestGitLabProviderProperties:
    def setup_method(self):
        from switchboard.git.providers.gitlab import GitLabProvider
        self.provider = GitLabProvider()


    def test_default_hostname(self):
        assert self.provider.default_hostname == "gitlab.com"


# ---------------------------------------------------------------------------
# Registry registration
# ---------------------------------------------------------------------------

class TestGitLabRegistration:
    def test_get_provider_gitlab(self):
        from switchboard.git.providers import get_provider
        p = get_provider("gitlab")
        assert p.name == "gitlab"

    def test_gitlab_in_providers_dict(self):
        from switchboard.git.providers import _PROVIDERS
        assert "gitlab" in _PROVIDERS

