"""Bitbucket Cloud provider implementation.

API tokens use Basic auth with the Atlassian account email (not the Bitbucket
username slug). The credential format is 'email:api_token'.

For git over HTTPS, the sentinel username 'x-bitbucket-api-token-auth' is used
so the Atlassian email is never embedded in clone/push URLs.
"""

import re

import httpx

from ouvrage.git.providers.base import (
    GitProvider, RepoInfo, PRResult, ValidationResult,
)

# SSH format: git@bitbucket.org:workspace/repo.git
_SSH_PATTERN = re.compile(r"^git@bitbucket\.org:([^/]+)/(.+?)(?:\.git)?$")
# HTTPS format: https://bitbucket.org/workspace/repo.git (optional auth embedded)
_HTTPS_PATTERN = re.compile(r"^https?://(?:[^@]+@)?bitbucket\.org/([^/]+)/(.+?)(?:\.git)?$")
# PR URL: https://bitbucket.org/{workspace}/{repo}/pull-requests/{id}
_PR_URL_RE = re.compile(
    r"^https?://bitbucket\.org/([^/]+)/([^/]+)/pull-requests/(\d+)"
)

_API_BASE = "https://api.bitbucket.org/2.0"

# Sentinel username for git-over-HTTPS with API tokens (no email in URL)
_GIT_AUTH_USERNAME = "x-bitbucket-api-token-auth"


def _basic_auth(credential: str) -> tuple[str, str]:
    """Split 'email:api_token' credential into (email, token).

    Raises ValueError if no colon is present.
    """
    if ":" not in credential:
        raise ValueError(
            "Bitbucket credential must be in 'email:api_token' format. "
            "Use your Atlassian account email and an API token created at "
            "https://id.atlassian.com/manage-profile/security/api-tokens"
        )
    email, _, token = credential.partition(":")
    return email, token


class BitbucketProvider(GitProvider):
    """Bitbucket Cloud git hosting provider."""

    @property
    def name(self) -> str:
        return "bitbucket"

    @property
    def default_hostname(self) -> str:
        return "bitbucket.org"

    def parse_repo_url(self, url: str) -> RepoInfo:
        """Parse Bitbucket repo URL (SSH or HTTPS) into RepoInfo.

        Handles:
          - git@bitbucket.org:workspace/repo.git
          - https://bitbucket.org/workspace/repo.git
        """
        m = _SSH_PATTERN.match(url)
        if m:
            return RepoInfo(owner=m.group(1), repo=m.group(2), hostname="bitbucket.org")

        m = _HTTPS_PATTERN.match(url)
        if m:
            return RepoInfo(owner=m.group(1), repo=m.group(2), hostname="bitbucket.org")

        raise ValueError(f"Cannot parse Bitbucket workspace/repo from URL: {url}")

    def build_authenticated_url(self, repo_url: str, credential: str) -> str:
        """Build HTTPS push URL using the API token sentinel username.

        The credential must be in 'email:api_token' format.
        The Atlassian email is NOT embedded in the URL — the sentinel username
        'x-bitbucket-api-token-auth' is used instead, as documented by Atlassian.
        """
        info = self.parse_repo_url(repo_url)
        _email, token = _basic_auth(credential)
        return f"https://{_GIT_AUTH_USERNAME}:{token}@bitbucket.org/{info.owner}/{info.repo}.git"

    async def validate_access(self, credential: str, repo_info: RepoInfo) -> ValidationResult:
        """Validate API token can access the repo via Bitbucket API.

        Calls GET /2.0/user to resolve the Bitbucket username, then checks
        repository access. The credential must be in 'email:api_token' format.
        """
        try:
            email, token = _basic_auth(credential)
        except ValueError as e:
            return ValidationResult(valid=False, error=str(e))

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Resolve actual account username from the API
                user_resp = await client.get(
                    f"{_API_BASE}/user",
                    auth=(email, token),
                )
                if user_resp.status_code == 401:
                    return ValidationResult(
                        valid=False,
                        error="API token is invalid or revoked",
                    )
                if user_resp.status_code == 403:
                    return ValidationResult(
                        valid=False,
                        error="API token lacks required permissions",
                    )
                if user_resp.status_code != 200:
                    return ValidationResult(
                        valid=False,
                        error=f"Bitbucket API returned {user_resp.status_code} for /user",
                    )
                user_data = user_resp.json()
                resolved_username = user_data.get("username") or user_data.get("account_id")

                # Check repository access
                repo_resp = await client.get(
                    f"{_API_BASE}/repositories/{repo_info.owner}/{repo_info.repo}",
                    auth=(email, token),
                )
                if repo_resp.status_code == 404:
                    return ValidationResult(
                        valid=False,
                        error="Repository not found or API token lacks access",
                    )
                if repo_resp.status_code in (401, 403):
                    return ValidationResult(
                        valid=False,
                        error="API token lacks read access to this repository",
                    )
                if repo_resp.status_code != 200:
                    return ValidationResult(
                        valid=False,
                        error=f"Bitbucket API returned {repo_resp.status_code} for repository check",
                    )

                return ValidationResult(
                    valid=True,
                    username=str(resolved_username) if resolved_username else None,
                )

        except Exception as e:
            return ValidationResult(valid=False, error=str(e))

    async def create_pr(
        self, credential: str, repo_info: RepoInfo,
        head: str, base: str, title: str, body: str = "",
    ) -> PRResult:
        """Create a Bitbucket pull request via REST API.

        Handles the case where a PR already exists for the source branch.
        """
        email, token = _basic_auth(credential)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_API_BASE}/repositories/{repo_info.owner}/{repo_info.repo}/pullrequests",
                auth=(email, token),
                json={
                    "title": title,
                    "description": body,
                    "source": {"branch": {"name": head}},
                    "destination": {"branch": {"name": base}},
                },
            )

            if resp.status_code == 201:
                data = resp.json()
                return PRResult(url=data["links"]["html"]["href"], number=data["id"])

            # Bitbucket returns 400 when a PR already exists for the source branch
            if resp.status_code == 400:
                error_body = resp.json()
                error_msg = str(error_body).lower()
                if "already" in error_msg or "duplicate" in error_msg:
                    return await self._find_existing_pr(
                        client, repo_info.owner, repo_info.repo, head, email, token,
                    )
                raise ValueError(f"PR creation failed: {error_body}")

            if resp.status_code == 404:
                raise ValueError(
                    f"Repository not found: {repo_info.owner}/{repo_info.repo}"
                )
            if resp.status_code == 403:
                raise ValueError(
                    "API token lacks permission to create pull requests. "
                    "Ensure the token has the 'write:pullrequest:bitbucket' scope."
                )

            resp.raise_for_status()
            return PRResult(url="", number=0)  # unreachable

    async def _find_existing_pr(
        self,
        client: httpx.AsyncClient,
        workspace: str,
        repo: str,
        source_branch: str,
        email: str,
        token: str,
    ) -> PRResult:
        """Find an existing open PR for the given source branch."""
        resp = await client.get(
            f"{_API_BASE}/repositories/{workspace}/{repo}/pullrequests",
            auth=(email, token),
            params={"q": f'source.branch.name="{source_branch}" AND state="OPEN"'},
        )
        if resp.status_code == 200:
            data = resp.json()
            values = data.get("values", [])
            if values:
                pr = values[0]
                return PRResult(url=pr["links"]["html"]["href"], number=pr["id"])
        raise ValueError(
            f"PR already exists for {source_branch} but could not find it"
        )

    async def get_pr_status(
        self, credential: str, repo_info: RepoInfo, pr_number: int,
    ) -> dict:
        """Get PR status from Bitbucket API. Maps Bitbucket states to internal format.

        Bitbucket states: OPEN, MERGED, DECLINED, SUPERSEDED
        """
        email, token = _basic_auth(credential)

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_API_BASE}/repositories/{repo_info.owner}/{repo_info.repo}"
                f"/pullrequests/{pr_number}",
                auth=(email, token),
            )
            if resp.status_code == 200:
                data = resp.json()
                bb_state = data.get("state", "OPEN")
                if bb_state == "MERGED":
                    return {"state": "merged", "merged": True}
                if bb_state in ("DECLINED", "SUPERSEDED"):
                    return {"state": "closed", "merged": False}
                # OPEN and any unknown states → open
                return {"state": "open", "merged": False}
            resp.raise_for_status()
            return {}

    def parse_pr_url(self, pr_url: str) -> tuple[RepoInfo, int]:
        """Parse a Bitbucket PR URL into (RepoInfo, pr_number).

        Handles: https://bitbucket.org/{workspace}/{repo}/pull-requests/{id}
        Raises ValueError if the URL doesn't match.
        """
        m = _PR_URL_RE.match(pr_url.strip())
        if not m:
            raise ValueError(f"Cannot parse Bitbucket PR URL: {pr_url!r}")
        info = RepoInfo(
            owner=m.group(1),
            repo=m.group(2),
            hostname="bitbucket.org",
        )
        return info, int(m.group(3))
