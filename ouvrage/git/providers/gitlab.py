"""GitLab provider implementation — supports gitlab.com and self-hosted instances."""

import re
from urllib.parse import quote

import httpx

from ouvrage.git.providers.base import (
    GitProvider, RepoInfo, PRResult, ValidationResult,
)

# SSH format: git@hostname:group/subgroup/project.git
_SSH_PATTERN = re.compile(r"^git@([^:]+):(.+?)(?:\.git)?$")
# HTTPS format: https://hostname/group/subgroup/project.git
_HTTPS_PATTERN = re.compile(r"^https?://([^/]+)/(.+?)(?:\.git)?$")
# MR URL: https://hostname/group/.../project/-/merge_requests/42
_MR_URL_RE = re.compile(
    r"^https?://([^/]+)/(.+)/-/merge_requests/(\d+)$"
)


def _split_path(path: str) -> tuple[str, str]:
    """Split a GitLab path into (owner, repo).

    For 'group/project' → ('group', 'project')
    For 'group/subgroup/project' → ('group/subgroup', 'project')
    """
    parts = path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"GitLab path must have at least namespace/project: {path!r}")
    return "/".join(parts[:-1]), parts[-1]


def _encode_path(owner: str, repo: str) -> str:
    """URL-encode a GitLab project path for use in API URLs."""
    return quote(f"{owner}/{repo}", safe="")


class GitLabProvider(GitProvider):
    """GitLab git hosting provider — supports gitlab.com and self-hosted instances."""

    @property
    def name(self) -> str:
        return "gitlab"

    @property
    def default_hostname(self) -> str:
        return "gitlab.com"

    def parse_repo_url(self, url: str) -> RepoInfo:
        """Parse GitLab repo URL (SSH or HTTPS) into RepoInfo.

        Handles nested groups: gitlab.com/a/b/c/project
        """
        m = _SSH_PATTERN.match(url)
        if m:
            hostname = m.group(1).lower()
            owner, repo = _split_path(m.group(2))
            return RepoInfo(owner=owner, repo=repo, hostname=hostname)

        m = _HTTPS_PATTERN.match(url)
        if m:
            hostname = m.group(1).lower()
            owner, repo = _split_path(m.group(2))
            return RepoInfo(owner=owner, repo=repo, hostname=hostname)

        raise ValueError(f"Cannot parse GitLab owner/repo from URL: {url}")

    def build_authenticated_url(self, repo_url: str, credential: str) -> str:
        """Build HTTPS push URL with token embedded as oauth2 credential."""
        info = self.parse_repo_url(repo_url)
        return f"https://oauth2:{credential}@{info.hostname}/{info.owner}/{info.repo}.git"

    def _api_base(self, hostname: str) -> str:
        """Construct GitLab API base URL for any hostname."""
        return f"https://{hostname}/api/v4"

    async def validate_access(self, credential: str, repo_info: RepoInfo) -> ValidationResult:
        """Validate token can access the repo via GitLab API.

        Checks project access level >= 30 (Developer).
        Also calls token introspection to get username for display.
        """
        headers = {"PRIVATE-TOKEN": credential}
        api_base = self._api_base(repo_info.hostname)
        encoded = _encode_path(repo_info.owner, repo_info.repo)

        try:
            async with httpx.AsyncClient(timeout=15, headers=headers) as client:
                # Check project access
                resp = await client.get(f"{api_base}/projects/{encoded}")

                if resp.status_code == 404:
                    return ValidationResult(
                        valid=False,
                        error="Repository not found or token lacks access",
                    )
                if resp.status_code in (401, 403):
                    return ValidationResult(
                        valid=False,
                        error="Token is invalid or lacks permissions",
                    )
                if resp.status_code != 200:
                    return ValidationResult(
                        valid=False,
                        error=f"GitLab API returned {resp.status_code}",
                    )

                data = resp.json()
                # Check access level — permissions may be null for public repos
                permissions = data.get("permissions") or {}
                project_access = permissions.get("project_access") or {}
                group_access = permissions.get("group_access") or {}
                access_level = max(
                    project_access.get("access_level") or 0,
                    group_access.get("access_level") or 0,
                )
                # If both are 0/null, the user may be a namespace owner or it's a public repo.
                # Fall through to token introspection to get username.
                if access_level > 0 and access_level < 30:
                    return ValidationResult(
                        valid=False,
                        error=f"Token lacks Developer access (access_level={access_level}, need >=30)",
                    )

                # Token introspection for username
                username = None
                try:
                    tok_resp = await client.get(f"{api_base}/personal_access_tokens/self")
                    if tok_resp.status_code == 200:
                        tok_data = tok_resp.json()
                        username = tok_data.get("name") or tok_data.get("user_id")
                except Exception:
                    pass  # Introspection failure is non-fatal

                return ValidationResult(valid=True, username=str(username) if username else None)

        except Exception as e:
            return ValidationResult(valid=False, error=str(e))

    async def create_pr(
        self, credential: str, repo_info: RepoInfo,
        head: str, base: str, title: str, body: str = "",
    ) -> PRResult:
        """Create a GitLab merge request via REST API.

        Handles the case where an MR already exists for the source branch.
        """
        headers = {"PRIVATE-TOKEN": credential}
        api_base = self._api_base(repo_info.hostname)
        encoded = _encode_path(repo_info.owner, repo_info.repo)

        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            resp = await client.post(
                f"{api_base}/projects/{encoded}/merge_requests",
                json={
                    "source_branch": head,
                    "target_branch": base,
                    "title": title,
                    "description": body,
                },
            )

            if resp.status_code == 201:
                data = resp.json()
                return PRResult(url=data["web_url"], number=data["iid"])

            # GitLab returns 409 or 422 when an MR already exists for the source branch
            if resp.status_code in (409, 422):
                error_body = resp.json()
                if "already exists" in str(error_body).lower():
                    return await self._find_existing_mr(client, api_base, encoded, head)
                raise ValueError(f"MR creation failed: {error_body}")

            if resp.status_code == 404:
                raise ValueError(
                    f"Repository not found: {repo_info.owner}/{repo_info.repo}"
                )
            if resp.status_code == 403:
                raise ValueError(
                    "Token lacks permission to create merge requests. "
                    "Ensure it has at least Developer access with api scope."
                )

            resp.raise_for_status()
            return PRResult(url="", number=0)  # unreachable

    async def _find_existing_mr(
        self,
        client: httpx.AsyncClient,
        api_base: str,
        encoded_path: str,
        source_branch: str,
    ) -> PRResult:
        """Find an existing open MR for the given source branch."""
        resp = await client.get(
            f"{api_base}/projects/{encoded_path}/merge_requests",
            params={"source_branch": source_branch, "state": "opened"},
        )
        if resp.status_code == 200:
            mrs = resp.json()
            if mrs:
                return PRResult(url=mrs[0]["web_url"], number=mrs[0]["iid"])
        raise ValueError(
            f"MR already exists for {source_branch} but could not find it"
        )

    async def get_pr_status(
        self, credential: str, repo_info: RepoInfo, pr_number: int,
    ) -> dict:
        """Get MR status from GitLab API. Maps GitLab states to internal format."""
        headers = {"PRIVATE-TOKEN": credential}
        api_base = self._api_base(repo_info.hostname)
        encoded = _encode_path(repo_info.owner, repo_info.repo)

        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            resp = await client.get(
                f"{api_base}/projects/{encoded}/merge_requests/{pr_number}"
            )
            if resp.status_code == 200:
                data = resp.json()
                gl_state = data.get("state", "opened")
                # Map GitLab states to internal format
                if gl_state == "merged":
                    return {"state": "merged", "merged": True}
                if gl_state == "closed":
                    return {"state": "closed", "merged": False}
                # "opened" and any unknown states → open
                return {"state": "open", "merged": False}
            resp.raise_for_status()
            return {}

    def parse_pr_url(self, pr_url: str) -> tuple[RepoInfo, int]:
        """Parse a GitLab MR URL into (RepoInfo, mr_iid).

        Handles: https://{hostname}/{namespace/project}/-/merge_requests/{iid}
        Raises ValueError if the URL doesn't match.
        """
        m = _MR_URL_RE.match(pr_url.strip())
        if not m:
            raise ValueError(f"Cannot parse GitLab MR URL: {pr_url!r}")
        hostname = m.group(1).lower()
        path = m.group(2)
        iid = int(m.group(3))
        owner, repo = _split_path(path)
        return RepoInfo(owner=owner, repo=repo, hostname=hostname), iid
