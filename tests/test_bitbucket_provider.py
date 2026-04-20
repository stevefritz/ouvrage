"""Tests for BitbucketProvider — URL parsing, auth, PR creation, status."""

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


def _make_async_client(get_responses=None, post_responses=None) -> MagicMock:
    """Create a mock async httpx client with configurable get/post responses."""
    client = AsyncMock()
    if get_responses is not None:
        client.get = AsyncMock(side_effect=get_responses)
    if post_responses is not None:
        client.post = AsyncMock(side_effect=post_responses)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


# ---------------------------------------------------------------------------
# parse_repo_url
# ---------------------------------------------------------------------------

class TestBitbucketParseRepoUrl:
    def setup_method(self):
        from ouvrage.git.providers.bitbucket import BitbucketProvider
        self.provider = BitbucketProvider()

    def test_https_with_git_suffix(self):
        info = self.provider.parse_repo_url("https://bitbucket.org/acme/widgets.git")
        assert info.owner == "acme"
        assert info.repo == "widgets"
        assert info.hostname == "bitbucket.org"

    def test_https_no_git_suffix(self):
        info = self.provider.parse_repo_url("https://bitbucket.org/acme/widgets")
        assert info.owner == "acme"
        assert info.repo == "widgets"
        assert info.hostname == "bitbucket.org"

    def test_ssh_with_git_suffix(self):
        info = self.provider.parse_repo_url("git@bitbucket.org:acme/widgets.git")
        assert info.owner == "acme"
        assert info.repo == "widgets"
        assert info.hostname == "bitbucket.org"

    def test_ssh_no_git_suffix(self):
        info = self.provider.parse_repo_url("git@bitbucket.org:acme/widgets")
        assert info.owner == "acme"
        assert info.repo == "widgets"
        assert info.hostname == "bitbucket.org"

    def test_https_with_embedded_auth(self):
        # URLs with embedded credentials should still parse correctly
        info = self.provider.parse_repo_url(
            "https://myuser:mypass@bitbucket.org/workspace/myrepo.git"
        )
        assert info.owner == "workspace"
        assert info.repo == "myrepo"
        assert info.hostname == "bitbucket.org"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse Bitbucket"):
            self.provider.parse_repo_url("https://github.com/acme/repo.git")

    def test_invalid_scheme_raises(self):
        with pytest.raises(ValueError, match="Cannot parse Bitbucket"):
            self.provider.parse_repo_url("not-a-url")

    def test_provider_name(self):
        assert self.provider.name == "bitbucket"

    def test_default_hostname(self):
        assert self.provider.default_hostname == "bitbucket.org"


# ---------------------------------------------------------------------------
# build_authenticated_url
# ---------------------------------------------------------------------------

class TestBitbucketBuildAuthenticatedUrl:
    def setup_method(self):
        from ouvrage.git.providers.bitbucket import BitbucketProvider
        self.provider = BitbucketProvider()

    def test_basic(self):
        url = self.provider.build_authenticated_url(
            "https://bitbucket.org/acme/widgets.git",
            "user@example.com:myapitoken",
        )
        assert url == "https://x-bitbucket-api-token-auth:myapitoken@bitbucket.org/acme/widgets.git"

    def test_from_ssh_url(self):
        url = self.provider.build_authenticated_url(
            "git@bitbucket.org:acme/widgets.git",
            "user@example.com:myapitoken",
        )
        assert url == "https://x-bitbucket-api-token-auth:myapitoken@bitbucket.org/acme/widgets.git"

    def test_token_with_colon(self):
        """Only split on first colon — email may contain no colons but token might."""
        url = self.provider.build_authenticated_url(
            "https://bitbucket.org/acme/widgets.git",
            "user@example.com:token:with:colons",
        )
        assert url == "https://x-bitbucket-api-token-auth:token:with:colons@bitbucket.org/acme/widgets.git"

    def test_email_not_in_url(self):
        """Atlassian email must NOT appear in the authenticated URL."""
        url = self.provider.build_authenticated_url(
            "https://bitbucket.org/acme/widgets.git",
            "secret@corp.com:myapitoken",
        )
        assert "secret@corp.com" not in url
        assert "x-bitbucket-api-token-auth" in url

    def test_missing_colon_raises(self):
        with pytest.raises(ValueError, match="email:api_token"):
            self.provider.build_authenticated_url(
                "https://bitbucket.org/acme/widgets.git",
                "nocolon",
            )


# ---------------------------------------------------------------------------
# validate_access
# ---------------------------------------------------------------------------

class TestBitbucketValidateAccess:
    def setup_method(self):
        from ouvrage.git.providers.bitbucket import BitbucketProvider
        from ouvrage.git.providers.base import RepoInfo
        self.provider = BitbucketProvider()
        self.repo_info = RepoInfo(owner="acme", repo="widgets", hostname="bitbucket.org")
        self.credential = "user@example.com:myapitoken"

    async def test_valid_credential(self):
        user_resp = _make_response(200, {"username": "myuser", "account_id": "abc123"})
        repo_resp = _make_response(200, {"full_name": "acme/widgets"})
        mock_client = _make_async_client(get_responses=[user_resp, repo_resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await self.provider.validate_access(self.credential, self.repo_info)

        assert result.valid is True
        assert result.username == "myuser"
        assert result.error is None

    async def test_invalid_api_token(self):
        user_resp = _make_response(401, {"type": "error", "error": {"message": "Unauthorized"}})
        mock_client = _make_async_client(get_responses=[user_resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await self.provider.validate_access(self.credential, self.repo_info)

        assert result.valid is False
        assert "invalid or revoked" in result.error

    async def test_insufficient_permissions(self):
        user_resp = _make_response(403, {"type": "error"})
        mock_client = _make_async_client(get_responses=[user_resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await self.provider.validate_access(self.credential, self.repo_info)

        assert result.valid is False
        assert "permissions" in result.error

    async def test_repo_not_found(self):
        user_resp = _make_response(200, {"username": "myuser"})
        repo_resp = _make_response(404, {"type": "error"})
        mock_client = _make_async_client(get_responses=[user_resp, repo_resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await self.provider.validate_access(self.credential, self.repo_info)

        assert result.valid is False
        assert "not found" in result.error

    async def test_repo_access_denied(self):
        user_resp = _make_response(200, {"username": "myuser"})
        repo_resp = _make_response(403, {"type": "error"})
        mock_client = _make_async_client(get_responses=[user_resp, repo_resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await self.provider.validate_access(self.credential, self.repo_info)

        assert result.valid is False
        assert "read access" in result.error

    async def test_user_api_error(self):
        user_resp = _make_response(500, {})
        mock_client = _make_async_client(get_responses=[user_resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await self.provider.validate_access(self.credential, self.repo_info)

        assert result.valid is False
        assert "500" in result.error

    async def test_missing_colon_in_credential(self):
        result = await self.provider.validate_access("nocolon", self.repo_info)
        assert result.valid is False
        assert "email:api_token" in result.error

    async def test_network_error(self):
        mock_client = _make_async_client(get_responses=[Exception("Connection refused")])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await self.provider.validate_access(self.credential, self.repo_info)

        assert result.valid is False
        assert "Connection refused" in result.error

    async def test_username_from_account_id_fallback(self):
        """Falls back to account_id if username field is missing."""
        user_resp = _make_response(200, {"account_id": "abc123"})
        repo_resp = _make_response(200, {"full_name": "acme/widgets"})
        mock_client = _make_async_client(get_responses=[user_resp, repo_resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await self.provider.validate_access(self.credential, self.repo_info)

        assert result.valid is True
        assert result.username == "abc123"

    async def test_api_called_with_email_not_username(self):
        """REST API auth uses email (not username slug) as the Basic auth username."""
        user_resp = _make_response(200, {"username": "bbslug"})
        repo_resp = _make_response(200, {"full_name": "acme/widgets"})
        mock_client = _make_async_client(get_responses=[user_resp, repo_resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            await self.provider.validate_access("user@atlassian.com:mytoken", self.repo_info)

        # First call to GET /user must use email as the username in Basic auth
        call_kwargs = mock_client.get.call_args_list[0]
        assert call_kwargs.kwargs["auth"] == ("user@atlassian.com", "mytoken")


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------

class TestBitbucketCreatePR:
    def setup_method(self):
        from ouvrage.git.providers.bitbucket import BitbucketProvider
        from ouvrage.git.providers.base import RepoInfo
        self.provider = BitbucketProvider()
        self.repo_info = RepoInfo(owner="acme", repo="widgets", hostname="bitbucket.org")
        self.credential = "user@example.com:myapitoken"

    async def test_create_pr_success(self):
        pr_data = {
            "id": 42,
            "title": "My PR",
            "links": {"html": {"href": "https://bitbucket.org/acme/widgets/pull-requests/42"}},
        }
        pr_resp = _make_response(201, pr_data)
        mock_client = _make_async_client(post_responses=[pr_resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await self.provider.create_pr(
                self.credential, self.repo_info,
                head="feature/new", base="main", title="My PR", body="Description",
            )

        assert result.number == 42
        assert result.url == "https://bitbucket.org/acme/widgets/pull-requests/42"

    async def test_create_pr_duplicate_finds_existing(self):
        error_resp = _make_response(400, {
            "type": "error",
            "error": {"message": "There is already a pull request for this branch."},
        })
        existing_pr = {
            "id": 7,
            "links": {"html": {"href": "https://bitbucket.org/acme/widgets/pull-requests/7"}},
        }
        list_resp = _make_response(200, {"values": [existing_pr], "pagelen": 10})

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=[error_resp])
        mock_client.get = AsyncMock(side_effect=[list_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await self.provider.create_pr(
                self.credential, self.repo_info,
                head="feature/new", base="main", title="My PR",
            )

        assert result.number == 7
        assert "pull-requests/7" in result.url

    async def test_create_pr_400_non_duplicate_raises(self):
        error_resp = _make_response(400, {
            "type": "error",
            "error": {"message": "Source branch not found."},
        })
        mock_client = _make_async_client(post_responses=[error_resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="PR creation failed"):
                await self.provider.create_pr(
                    self.credential, self.repo_info,
                    head="nonexistent", base="main", title="Bad PR",
                )

    async def test_create_pr_repo_not_found(self):
        resp = _make_response(404, {"type": "error"})
        mock_client = _make_async_client(post_responses=[resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="Repository not found"):
                await self.provider.create_pr(
                    self.credential, self.repo_info,
                    head="feature", base="main", title="PR",
                )

    async def test_create_pr_forbidden(self):
        resp = _make_response(403, {"type": "error"})
        mock_client = _make_async_client(post_responses=[resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="lacks permission"):
                await self.provider.create_pr(
                    self.credential, self.repo_info,
                    head="feature", base="main", title="PR",
                )

    async def test_create_pr_forbidden_scope_message(self):
        """403 error message references API token scope, not app password."""
        resp = _make_response(403, {"type": "error"})
        mock_client = _make_async_client(post_responses=[resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="write:pullrequest:bitbucket"):
                await self.provider.create_pr(
                    self.credential, self.repo_info,
                    head="feature", base="main", title="PR",
                )

    async def test_create_pr_duplicate_no_existing_raises(self):
        """400 duplicate but listing returns empty — raise descriptive error."""
        error_resp = _make_response(400, {
            "type": "error",
            "error": {"message": "already exists for this branch"},
        })
        list_resp = _make_response(200, {"values": [], "pagelen": 10})

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=[error_resp])
        mock_client.get = AsyncMock(side_effect=[list_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ValueError, match="could not find it"):
                await self.provider.create_pr(
                    self.credential, self.repo_info,
                    head="feature/new", base="main", title="My PR",
                )


# ---------------------------------------------------------------------------
# get_pr_status
# ---------------------------------------------------------------------------

class TestBitbucketGetPrStatus:
    def setup_method(self):
        from ouvrage.git.providers.bitbucket import BitbucketProvider
        from ouvrage.git.providers.base import RepoInfo
        self.provider = BitbucketProvider()
        self.repo_info = RepoInfo(owner="acme", repo="widgets", hostname="bitbucket.org")
        self.credential = "user@example.com:myapitoken"

    async def test_open_state(self):
        resp = _make_response(200, {"id": 1, "state": "OPEN"})
        mock_client = _make_async_client(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            status = await self.provider.get_pr_status(self.credential, self.repo_info, 1)

        assert status["state"] == "open"
        assert status["merged"] is False

    async def test_merged_state(self):
        resp = _make_response(200, {"id": 1, "state": "MERGED"})
        mock_client = _make_async_client(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            status = await self.provider.get_pr_status(self.credential, self.repo_info, 1)

        assert status["state"] == "merged"
        assert status["merged"] is True

    async def test_declined_state(self):
        resp = _make_response(200, {"id": 1, "state": "DECLINED"})
        mock_client = _make_async_client(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            status = await self.provider.get_pr_status(self.credential, self.repo_info, 1)

        assert status["state"] == "closed"
        assert status["merged"] is False

    async def test_superseded_state(self):
        resp = _make_response(200, {"id": 1, "state": "SUPERSEDED"})
        mock_client = _make_async_client(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            status = await self.provider.get_pr_status(self.credential, self.repo_info, 1)

        assert status["state"] == "closed"
        assert status["merged"] is False

    async def test_unknown_state_maps_to_open(self):
        resp = _make_response(200, {"id": 1, "state": "UNKNOWN_FUTURE_STATE"})
        mock_client = _make_async_client(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            status = await self.provider.get_pr_status(self.credential, self.repo_info, 1)

        assert status["state"] == "open"
        assert status["merged"] is False

    async def test_api_error_raises(self):
        resp = _make_response(404, {"type": "error"})
        resp.raise_for_status = MagicMock(side_effect=Exception("404 Not Found"))
        mock_client = _make_async_client(get_responses=[resp])

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(Exception, match="404"):
                await self.provider.get_pr_status(self.credential, self.repo_info, 999)


# ---------------------------------------------------------------------------
# parse_pr_url
# ---------------------------------------------------------------------------

class TestBitbucketParsePrUrl:
    def setup_method(self):
        from ouvrage.git.providers.bitbucket import BitbucketProvider
        self.provider = BitbucketProvider()

    def test_valid_pr_url(self):
        info, number = self.provider.parse_pr_url(
            "https://bitbucket.org/acme/widgets/pull-requests/42"
        )
        assert info.owner == "acme"
        assert info.repo == "widgets"
        assert info.hostname == "bitbucket.org"
        assert number == 42

    def test_valid_pr_url_with_trailing_slash(self):
        info, number = self.provider.parse_pr_url(
            "https://bitbucket.org/acme/widgets/pull-requests/42/"
        )
        assert number == 42
        assert info.owner == "acme"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse Bitbucket PR URL"):
            self.provider.parse_pr_url("https://github.com/acme/widgets/pull/42")

    def test_gitlab_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse Bitbucket PR URL"):
            self.provider.parse_pr_url(
                "https://gitlab.com/acme/widgets/-/merge_requests/5"
            )

    def test_missing_number_raises(self):
        with pytest.raises(ValueError, match="Cannot parse Bitbucket PR URL"):
            self.provider.parse_pr_url("https://bitbucket.org/acme/widgets/pull-requests/")

    def test_large_pr_number(self):
        _, number = self.provider.parse_pr_url(
            "https://bitbucket.org/acme/widgets/pull-requests/9999"
        )
        assert number == 9999


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------

class TestBitbucketRegistry:
    def test_provider_registered(self):
        from ouvrage.git.providers import get_provider
        provider = get_provider("bitbucket")
        assert provider.name == "bitbucket"

    def test_detect_provider_bitbucket_org(self):
        from ouvrage.git.providers import _DEFAULT_HOSTNAMES
        assert _DEFAULT_HOSTNAMES.get("bitbucket.org") == "bitbucket"

    def test_provider_in_all(self):
        from ouvrage.git.providers import BitbucketProvider
        assert BitbucketProvider is not None
