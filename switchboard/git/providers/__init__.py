"""Git provider registry — detection, resolution, and provider lookup."""

import logging
from urllib.parse import urlparse

from switchboard.git.providers.base import GitProvider, RepoInfo, PRResult, ValidationResult
from switchboard.git.providers.github import GitHubProvider

log = logging.getLogger(__name__)

# Provider registry — maps provider name to instance
_PROVIDERS: dict[str, GitProvider] = {
    "github": GitHubProvider(),
}

# Hardcoded hostname → provider defaults (checked after DB credentials)
_DEFAULT_HOSTNAMES: dict[str, str] = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
}


def get_provider(name: str) -> GitProvider:
    """Get a provider instance by name. Raises ValueError if unknown."""
    provider = _PROVIDERS.get(name)
    if not provider:
        raise ValueError(
            f"Unknown git provider: '{name}'. "
            f"Available: {', '.join(_PROVIDERS.keys())}"
        )
    return provider


def _parse_hostname(url: str) -> str | None:
    """Extract hostname from a git URL (SSH or HTTPS)."""
    # SSH format: git@hostname:owner/repo.git
    if url.startswith("git@"):
        parts = url.split("@", 1)
        if len(parts) == 2:
            host_part = parts[1].split(":", 1)[0]
            return host_part.lower()
    # HTTPS format
    try:
        parsed = urlparse(url)
        if parsed.hostname:
            return parsed.hostname.lower()
    except Exception:
        pass
    return None


async def detect_provider(url: str) -> str | None:
    """Detect git provider from a repo URL.

    Checks git_credentials hostnames first (supports custom hosts),
    then falls back to hardcoded defaults.
    """
    hostname = _parse_hostname(url)
    if not hostname:
        return None

    # 1. Check git_credentials table for matching hostname
    try:
        from switchboard.db.git_credentials import get_credential_by_hostname
        cred = await get_credential_by_hostname(hostname)
        if cred:
            return cred["provider"]
    except Exception:
        pass  # DB not available (e.g. during import), fall through

    # 2. Check hardcoded defaults
    return _DEFAULT_HOSTNAMES.get(hostname)


async def resolve_credential(project: dict) -> tuple[GitProvider, str]:
    """Resolve git credential for a project.

    Resolution order:
    1. Project-level credential_override
    2. Instance-level credential from git_credentials table
    3. Legacy: instance.github_pat_encrypted (backward compat)
    4. ValueError if nothing found

    Returns (provider_instance, decrypted_credential).
    """
    from switchboard.crypto import decrypt_value, is_fernet_token
    from switchboard.db.git_credentials import get_credential_by_provider

    provider_name = project.get("provider") or "github"
    provider = get_provider(provider_name)

    # 1. Project-level override (credential_override or legacy github_pat_override)
    override = project.get("credential_override") or project.get("github_pat_override")
    if override:
        credential = decrypt_value(override) if is_fernet_token(override) else override
        return provider, credential

    # 2. Instance-level credential from git_credentials table
    cred_row = await get_credential_by_provider(provider_name)
    if cred_row and cred_row.get("credential"):
        credential = decrypt_value(cred_row["credential"]) if is_fernet_token(cred_row["credential"]) else cred_row["credential"]
        return provider, credential

    # 3. Legacy fallback: instance.github_pat_encrypted (for GitHub only)
    if provider_name == "github":
        try:
            from switchboard.db.users import get_instance_github_pat
            pat = await get_instance_github_pat()
            return provider, pat
        except ValueError:
            pass

    raise ValueError(
        f"No {provider_name} credential configured. "
        f"Add one in Settings or set credential_override on the project."
    )


__all__ = [
    "GitProvider", "RepoInfo", "PRResult", "ValidationResult",
    "GitHubProvider",
    "get_provider", "detect_provider", "resolve_credential",
    "_parse_hostname", "_DEFAULT_HOSTNAMES",
]
