"""Credential validation — shared logic for project-level access checks.

Used by:
- Layer 2: Post-create/update project validation (informational)
- Layer 3: Dispatch pre-flight gate (hard gate)
"""

import logging

from ouvrage.db._helpers import now_iso
from ouvrage.git.providers import resolve_credential

log = logging.getLogger(__name__)


async def validate_project_access(project: dict) -> dict:
    """Validate that a project's credential can access its repo.

    Returns a structured result:
        {
            "status": "validated" | "warning" | "error",
            "message": str,
            "checked_at": str (ISO timestamp),
        }

    - "validated": credential works and can access the repo
    - "warning": no credential configured (recoverable)
    - "error": credential exists but fails validation
    """
    checked_at = now_iso()

    # 1. Resolve credential
    try:
        provider, credential = await resolve_credential(project)
    except ValueError as e:
        return {
            "status": "warning",
            "message": f"No credential configured. {e}",
            "checked_at": checked_at,
        }

    # 2. Parse repo URL
    try:
        repo_info = provider.parse_repo_url(project["repo"])
    except ValueError as e:
        return {
            "status": "error",
            "message": f"Cannot parse repo URL for {provider.name}: {e}",
            "checked_at": checked_at,
        }

    # 3. Validate access via provider API
    try:
        result = await provider.validate_access(credential, repo_info)
    except Exception as e:
        log.warning("validate_project_access failed for %s: %s", project["id"], e)
        return {
            "status": "error",
            "message": f"Validation failed: {e}",
            "checked_at": checked_at,
        }

    if result.valid:
        username_note = f" (as {result.username})" if result.username else ""
        return {
            "status": "validated",
            "message": f"Credential validated{username_note} — can access {repo_info.owner}/{repo_info.repo}",
            "checked_at": checked_at,
        }
    else:
        return {
            "status": "error",
            "message": result.error or "Credential validation failed",
            "checked_at": checked_at,
        }
