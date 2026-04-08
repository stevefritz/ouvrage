"""GitProvider abstract base class and shared data types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RepoInfo:
    """Parsed repository information."""
    owner: str
    repo: str
    hostname: str


@dataclass
class PRResult:
    """Result of creating a pull request."""
    url: str
    number: int


@dataclass
class ValidationResult:
    """Result of validating credential access."""
    valid: bool
    username: str | None = None
    error: str | None = None


class GitProvider(ABC):
    """Abstract base class for git hosting providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g. 'github', 'gitlab', 'bitbucket')."""

    @property
    @abstractmethod
    def default_hostname(self) -> str:
        """Default hostname for this provider (e.g. 'github.com')."""

    @abstractmethod
    def parse_repo_url(self, url: str) -> RepoInfo:
        """Parse a repo URL into structured info. Raises ValueError if not parseable."""

    @abstractmethod
    def build_authenticated_url(self, repo_url: str, credential: str) -> str:
        """Build an authenticated HTTPS URL for git operations."""

    @abstractmethod
    async def validate_access(self, credential: str, repo_info: RepoInfo) -> ValidationResult:
        """Validate that a credential can access the given repo."""

    @abstractmethod
    async def create_pr(
        self, credential: str, repo_info: RepoInfo,
        head: str, base: str, title: str, body: str = "",
    ) -> PRResult:
        """Create a pull request. Returns PRResult."""

    @abstractmethod
    async def get_pr_status(
        self, credential: str, repo_info: RepoInfo, pr_number: int,
    ) -> dict:
        """Get pull request status. Returns dict with state, mergeable, etc."""
