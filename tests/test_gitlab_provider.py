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

    def test_https_simple(self):
        info = self.provider.parse_repo_url("https://gitlab.com/acme/widgets.git")
        assert info.owner == "acme"
        assert info.repo == "widgets"
        assert info.hostname == "gitlab.com"

    def test_https_no_git_suffix(self):
        info = self.provider.parse_repo_url("https://gitlab.com/acme/widgets")
        assert info.owner == "acme"
        assert info.repo == "widgets"
        assert info.hostname == "gitlab.com"

    def test_https_nested_one_subgroup(self):
        info = self.provider.parse_repo_url("https://gitlab.com/group/subgroup/project.git")
        assert info.owner == "group/subgroup"
        assert info.repo == "project"
        assert info.hostname == "gitlab.com"

    def test_https_nested_deep_groups(self):
        info = self.provider.parse_repo_url("https://gitlab.com/a/b/c/project.git")
        assert info.owner == "a/b/c"
        assert info.repo == "project"
        assert info.hostname == "gitlab.com"

    def test_ssh_simple(self):
        info = self.provider.parse_repo_url("git@gitlab.com:acme/widgets.git")
        assert info.owner == "acme"
        assert info.repo == "widgets"
        assert info.hostname == "gitlab.com"

    def test_ssh_nested_groups(self):
        info = self.provider.parse_repo_url("git@gitlab.com:group/subgroup/project.git")
        assert info.owner == "group/subgroup"
        assert info.repo == "project"
        assert info.hostname == "gitlab.com"

    def test_self_hosted_https(self):
        info = self.provider.parse_repo_url("https://gl.example.com/myorg/myrepo.git")
        assert info.hostname == "gl.example.com"
        assert info.owner == "myorg"
        assert info.repo == "myrepo"

    def test_self_hosted_ssh(self):
        info = self.provider.parse_repo_url("git@gl.sf.net:group/project.git")
        assert info.hostname == "gl.sf.net"
        assert info.owner == "group"
        assert info.repo == "project"

    def test_self_hosted_nested(self):
        info = self.provider.parse_repo_url("https://gl.sf.net/a/b/project.git")
        assert info.hostname == "gl.sf.net"
        assert info.owner == "a/b"
        assert info.repo == "project"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            self.provider.parse_repo_url("not-a-url")

    def test_invalid_single_segment_raises(self):
        """A path with only one segment (no namespace) is invalid."""
        with pytest.raises(ValueError):
            self.provider.parse_repo_url("https://gitlab.com/project-only")

    def test_hostname_lowercased(self):
        info = self.provider.parse_repo_url("https://GitLab.COM/org/repo.git")
        assert info.hostname == "gitlab.com"


# ---------------------------------------------------------------------------
# build_authenticated_url
# ---------------------------------------------------------------------------

class TestGitLabBuildAuthenticatedUrl:
    def setup_method(self):
        from switchboard.git.providers.gitlab import GitLabProvider
        self.provider = GitLabProvider()

    def test_https_simple(self):
        url = self.provider.build_authenticated_url(
            "https://gitlab.com/acme/widgets.git", "mytoken"
        )
        assert url == "https://oauth2:mytoken@gitlab.com/acme/widgets.git"

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

    def test_self_hosted(self):
        url = self.provider.build_authenticated_url(
            "https://gl.sf.net/group/project.git", "mytoken"
        )
        assert url == "https://oauth2:mytoken@gl.sf.net/group/project.git"

    def test_token_embedded(self):
        url = self.provider.build_authenticated_url(
            "https://gitlab.com/org/repo.git", "glpat-abc123"
        )
        assert "glpat-abc123" in url


# ---------------------------------------------------------------------------
# validate_access
# ---------------------------------------------------------------------------

class TestGitLabValidateAccess:
    def setup_method(self):
        from switchboard.git.providers.gitlab import GitLabProvider
        from switchboard.git.providers.base import RepoInfo
        self.provider = GitLabProvider()
        self.repo_info = RepoInfo(owner="acme", repo="widgets", hostname="gitlab.com")

    async def test_valid_developer_access(self):
        project_resp = _make_response(200, {
            "permissions": {
                "project_access": {"access_level": 40},  # Maintainer
                "group_access": None,
            }
        })
        token_resp = _make_response(200, {"name": "Alice", "user_id": 42})

        client = _make_async_client([project_resp, token_resp])
        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.validate_access("mytoken", self.repo_info)

        assert result.valid is True
        assert result.username == "Alice"

    async def test_valid_group_access(self):
        """Group access_level >= 30 also counts."""
        project_resp = _make_response(200, {
            "permissions": {
                "project_access": None,
                "group_access": {"access_level": 30},
            }
        })
        token_resp = _make_response(200, {"name": "Bob", "user_id": 7})

        client = _make_async_client([project_resp, token_resp])
        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.validate_access("mytoken", self.repo_info)

        assert result.valid is True
        assert result.username == "Bob"

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

    async def test_invalid_token_401(self):
        project_resp = _make_response(401, {"message": "Unauthorized"})

        client = _make_async_client([project_resp])
        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.validate_access("badtoken", self.repo_info)

        assert result.valid is False
        assert "invalid" in result.error.lower() or "permission" in result.error.lower()

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

    async def test_introspection_failure_is_non_fatal(self):
        """Token introspection failure doesn't break validation."""
        project_resp = _make_response(200, {
            "permissions": {
                "project_access": {"access_level": 40},
                "group_access": None,
            }
        })
        token_resp = _make_response(401, {"message": "Unauthorized"})

        client = _make_async_client([project_resp, token_resp])
        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.validate_access("mytoken", self.repo_info)

        assert result.valid is True
        # Username may be None since introspection failed
        assert result.username is None

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

    async def test_self_hosted_uses_correct_api_base(self):
        """Self-hosted instances get API calls to their hostname."""
        from switchboard.git.providers.base import RepoInfo
        self_hosted_repo = RepoInfo(
            owner="myorg", repo="myrepo", hostname="gl.sf.net"
        )
        project_resp = _make_response(200, {
            "permissions": {
                "project_access": {"access_level": 40},
                "group_access": None,
            }
        })
        token_resp = _make_response(200, {"name": "Dave", "user_id": 5})

        captured_urls = []

        async def capturing_get(url, **kwargs):
            captured_urls.append(url)
            if "personal_access_tokens" in url:
                return token_resp
            return project_resp

        client = AsyncMock()
        client.get = AsyncMock(side_effect=capturing_get)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.validate_access("mytoken", self_hosted_repo)

        assert result.valid is True
        assert all("gl.sf.net" in url for url in captured_urls)
        assert all("api/v4" in url for url in captured_urls)


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------

class TestGitLabCreatePr:
    def setup_method(self):
        from switchboard.git.providers.gitlab import GitLabProvider
        from switchboard.git.providers.base import RepoInfo
        self.provider = GitLabProvider()
        self.repo_info = RepoInfo(owner="acme", repo="widgets", hostname="gitlab.com")

    async def test_create_pr_success(self):
        mr_resp = _make_response(201, {
            "iid": 42,
            "web_url": "https://gitlab.com/acme/widgets/-/merge_requests/42",
        })

        client = AsyncMock()
        client.post = AsyncMock(return_value=mr_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            result = await self.provider.create_pr(
                "mytoken", self.repo_info,
                head="feature-branch", base="main",
                title="My Feature", body="Description",
            )

        assert result.number == 42
        assert result.url == "https://gitlab.com/acme/widgets/-/merge_requests/42"

    async def test_create_pr_payload(self):
        """Verify the correct payload is sent to the API."""
        mr_resp = _make_response(201, {
            "iid": 1,
            "web_url": "https://gitlab.com/acme/widgets/-/merge_requests/1",
        })

        client = AsyncMock()
        client.post = AsyncMock(return_value=mr_resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            await self.provider.create_pr(
                "mytoken", self.repo_info,
                head="feat", base="main",
                title="Title", body="Body text",
            )

        call_kwargs = client.post.call_args[1]
        payload = call_kwargs["json"]
        assert payload["source_branch"] == "feat"
        assert payload["target_branch"] == "main"
        assert payload["title"] == "Title"
        assert payload["description"] == "Body text"

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

    async def test_create_pr_already_exists_422(self):
        """422 with 'already exists' — finds existing MR."""
        conflict_resp = _make_response(422, {"message": ["Another open merge request already exists"]})
        list_resp = _make_response(200, [{
            "iid": 5,
            "web_url": "https://gitlab.com/acme/widgets/-/merge_requests/5",
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

        assert result.number == 5

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

    async def test_self_hosted_uses_correct_hostname(self):
        from switchboard.git.providers.base import RepoInfo
        self_hosted = RepoInfo(owner="org", repo="repo", hostname="gl.sf.net")
        resp = _make_response(200, {"state": "opened"})

        captured_urls = []

        async def capturing_get(url, **kwargs):
            captured_urls.append(url)
            return resp

        client = AsyncMock()
        client.get = AsyncMock(side_effect=capturing_get)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=client):
            await self.provider.get_pr_status("tok", self_hosted, 5)

        assert "gl.sf.net" in captured_urls[0]
        assert "api/v4" in captured_urls[0]


# ---------------------------------------------------------------------------
# parse_pr_url
# ---------------------------------------------------------------------------

class TestGitLabParsePrUrl:
    def setup_method(self):
        from switchboard.git.providers.gitlab import GitLabProvider
        self.provider = GitLabProvider()

    def test_standard_url(self):
        info, number = self.provider.parse_pr_url(
            "https://gitlab.com/acme/widgets/-/merge_requests/42"
        )
        assert info.owner == "acme"
        assert info.repo == "widgets"
        assert info.hostname == "gitlab.com"
        assert number == 42

    def test_nested_groups(self):
        info, number = self.provider.parse_pr_url(
            "https://gitlab.com/group/subgroup/project/-/merge_requests/7"
        )
        assert info.owner == "group/subgroup"
        assert info.repo == "project"
        assert info.hostname == "gitlab.com"
        assert number == 7

    def test_deep_nested_groups(self):
        info, number = self.provider.parse_pr_url(
            "https://gitlab.com/a/b/c/project/-/merge_requests/1"
        )
        assert info.owner == "a/b/c"
        assert info.repo == "project"
        assert number == 1

    def test_self_hosted(self):
        info, number = self.provider.parse_pr_url(
            "https://gl.sf.net/myorg/myrepo/-/merge_requests/99"
        )
        assert info.hostname == "gl.sf.net"
        assert info.owner == "myorg"
        assert info.repo == "myrepo"
        assert number == 99

    def test_trailing_whitespace_stripped(self):
        info, number = self.provider.parse_pr_url(
            "  https://gitlab.com/org/repo/-/merge_requests/3  "
        )
        assert number == 3

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            self.provider.parse_pr_url("https://github.com/org/repo/pull/1")

    def test_missing_merge_requests_raises(self):
        with pytest.raises(ValueError):
            self.provider.parse_pr_url("https://gitlab.com/org/repo")

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

    def test_name(self):
        assert self.provider.name == "gitlab"

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

    def test_gitlab_provider_is_gitlab_instance(self):
        from switchboard.git.providers import get_provider
        from switchboard.git.providers.gitlab import GitLabProvider
        p = get_provider("gitlab")
        assert isinstance(p, GitLabProvider)
