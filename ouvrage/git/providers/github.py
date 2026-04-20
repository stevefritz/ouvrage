"""GitHub provider implementation — extracted from operations.py."""

import re

import httpx

from ouvrage.git.providers.base import (
    GitProvider, RepoInfo, PRResult, ValidationResult,
)

_SSH_PATTERN = re.compile(r"^git@github\.com:([^/]+)/(.+?)(?:\.git)?$")
_HTTPS_PATTERN = re.compile(r"^https?://github\.com/([^/]+)/(.+?)(?:\.git)?$")
_PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)


class GitHubProvider(GitProvider):
    """GitHub git hosting provider."""

    @property
    def name(self) -> str:
        return "github"

    @property
    def default_hostname(self) -> str:
        return "github.com"

    def parse_repo_url(self, url: str) -> RepoInfo:
        """Parse GitHub repo URL (SSH or HTTPS) into RepoInfo."""
        m = _SSH_PATTERN.match(url)
        if m:
            return RepoInfo(owner=m.group(1), repo=m.group(2), hostname="github.com")
        m = _HTTPS_PATTERN.match(url)
        if m:
            return RepoInfo(owner=m.group(1), repo=m.group(2), hostname="github.com")
        raise ValueError(f"Cannot parse GitHub owner/repo from URL: {url}")

    def build_authenticated_url(self, repo_url: str, credential: str) -> str:
        """Build HTTPS push URL with PAT embedded."""
        info = self.parse_repo_url(repo_url)
        return f"https://oauth2:{credential}@github.com/{info.owner}/{info.repo}.git"

    async def validate_access(self, credential: str, repo_info: RepoInfo) -> ValidationResult:
        """Validate PAT can access the repo via GitHub API."""
        headers = {
            "Authorization": f"Bearer {credential}",
            "Accept": "application/vnd.github+json",
        }
        try:
            async with httpx.AsyncClient(timeout=15, headers=headers) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{repo_info.owner}/{repo_info.repo}"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    owner = data.get("owner", {}).get("login")
                    return ValidationResult(valid=True, username=owner)
                if resp.status_code == 404:
                    return ValidationResult(valid=False, error="Repository not found or PAT lacks access")
                if resp.status_code in (401, 403):
                    return ValidationResult(valid=False, error="PAT is invalid or lacks permissions")
                return ValidationResult(valid=False, error=f"GitHub API returned {resp.status_code}")
        except Exception as e:
            return ValidationResult(valid=False, error=str(e))

    async def create_pr(
        self, credential: str, repo_info: RepoInfo,
        head: str, base: str, title: str, body: str = "",
    ) -> PRResult:
        """Create a GitHub PR via REST API. Handles 422 'already exists'."""
        headers = {
            "Authorization": f"Bearer {credential}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            resp = await client.post(
                f"https://api.github.com/repos/{repo_info.owner}/{repo_info.repo}/pulls",
                json={"title": title, "head": head, "base": base, "body": body},
            )

            if resp.status_code == 201:
                data = resp.json()
                return PRResult(url=data["html_url"], number=data["number"])

            if resp.status_code == 422:
                errors = resp.json()
                if "already exists" in str(errors).lower():
                    return await self._find_existing_pr(
                        client, repo_info.owner, repo_info.repo, head,
                    )
                raise ValueError(f"PR creation failed: {errors}")

            if resp.status_code == 404:
                raise ValueError(f"Repository not found: {repo_info.owner}/{repo_info.repo}")
            if resp.status_code == 403:
                raise ValueError("PAT lacks permission to create PRs. Ensure it has `repo` scope.")

            resp.raise_for_status()
            return PRResult(url="", number=0)  # unreachable

    async def _find_existing_pr(
        self, client: httpx.AsyncClient, owner: str, repo: str, head: str,
    ) -> PRResult:
        """Find an existing open PR for the given head branch."""
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            params={"head": f"{owner}:{head}", "state": "open"},
        )
        if resp.status_code == 200:
            prs = resp.json()
            if prs:
                return PRResult(url=prs[0]["html_url"], number=prs[0]["number"])
        raise ValueError(f"PR already exists for {head} but could not find it")

    async def get_pr_status(
        self, credential: str, repo_info: RepoInfo, pr_number: int,
    ) -> dict:
        """Get PR status from GitHub API."""
        headers = {
            "Authorization": f"Bearer {credential}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repo_info.owner}/{repo_info.repo}/pulls/{pr_number}"
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "state": data["state"],
                    "mergeable": data.get("mergeable"),
                    "merged": data.get("merged", False),
                    "draft": data.get("draft", False),
                    "url": data["html_url"],
                }
            resp.raise_for_status()
            return {}

    def parse_pr_url(self, pr_url: str) -> tuple[RepoInfo, int]:
        """Parse a GitHub PR URL into (RepoInfo, pr_number).

        Handles: https://github.com/{owner}/{repo}/pull/{number}
        Raises ValueError if the URL doesn't match.
        """
        m = _PR_URL_RE.match(pr_url.strip())
        if not m:
            raise ValueError(f"Cannot parse GitHub PR URL: {pr_url!r}")
        info = RepoInfo(owner=m.group("owner"), repo=m.group("repo"), hostname="github.com")
        return info, int(m.group("number"))
