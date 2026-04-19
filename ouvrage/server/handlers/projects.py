"""Project tool handlers."""

import logging
import os
import re
import shutil

import ouvrage.db as db
import ouvrage.dispatch as task_engine
from ouvrage.server.context import get_request_user_id
from ouvrage.git.operations import normalize_repo_url
from ouvrage.crypto import encrypt_value, is_fernet_token

log = logging.getLogger(__name__)

WORKTREE_BASE = os.environ.get("WORKTREE_BASE", "/work")


def _resolve_working_dir(repo: str, folder_name: str | None = None) -> str:
    """Derive working_dir from repo URL and optional folder name override."""
    if folder_name:
        name = folder_name
    else:
        # Extract repo name from URL: git@github.com:org/repo.git → repo
        name = repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        # Also handle ssh colon syntax: git@github.com:org/repo.git
        if ":" in name:
            name = name.rsplit(":", 1)[-1].removesuffix(".git")
    # Sanitize — no path traversal
    name = name.replace("/", "").replace("..", "").replace("\\", "")
    if not name:
        raise ValueError("Could not derive folder name from repo URL")
    return os.path.join(WORKTREE_BASE, name)


async def _run_project_validation(project_id: str, project: dict) -> dict:
    """Validate project credential access and store result. Returns updated project dict."""
    from ouvrage.git.validation import validate_project_access

    try:
        result = await validate_project_access(project)
        updated = await db.update_project(
            project_id,
            credential_status=result["status"],
            credential_status_message=result["message"],
            credential_checked_at=result["checked_at"],
        )
        return updated
    except Exception as e:
        log.warning("Credential validation failed for %s: %s", project_id, e)
        return project


async def _handle_create_project(arguments):
    max_projects = await db.get_max_projects()
    if max_projects > 0:
        current_count = await db.count_projects()
        if current_count >= max_projects:
            return {"error": f"Project limit reached ({current_count}/{max_projects}). Upgrade your plan for more projects."}

    folder_name = arguments.get("folder_name")
    if folder_name and not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$', folder_name):
        return {"error": "folder_name must be a folder name only (e.g. my-app). No paths or special characters."}

    repo = normalize_repo_url(arguments["repo"])
    working_dir = arguments.get("working_dir") or _resolve_working_dir(
        repo, folder_name
    )
    # Enforce worktree base — no escaping
    resolved = os.path.realpath(working_dir)
    base = os.path.realpath(WORKTREE_BASE)
    if not resolved.startswith(base + "/") and resolved != base:
        raise ValueError(f"working_dir must be under {WORKTREE_BASE}, got: {working_dir}")

    # Check for working_dir collision with existing projects
    existing = await db.list_projects()
    for p in existing:
        if os.path.realpath(p["working_dir"]) == resolved:
            raise ValueError(
                f"working_dir '{resolved}' already belongs to project '{p['id']}' "
                f"— use folder_name to override"
            )

    REQUIRED_PROJECT_CONFIG = ["model", "review_model", "auto_test", "auto_review", "auto_pr", "auto_merge", "max_turns", "max_wall_clock"]
    missing = [f for f in REQUIRED_PROJECT_CONFIG if arguments.get(f) is None]
    if missing:
        return {"error": f"Missing required config fields: {', '.join(missing)}. All config must be explicitly set at project creation."}

    # credential_override takes priority over deprecated github_pat_override
    cred_raw = arguments.get("credential_override") or arguments.get("github_pat_override")
    cred_last4 = cred_raw[-4:] if cred_raw and len(cred_raw) >= 4 else None
    cred_encrypted = encrypt_value(cred_raw) if cred_raw and not is_fernet_token(cred_raw) else cred_raw or None

    # Auto-detect provider from URL if not specified
    provider = arguments.get("provider")
    if not provider:
        from ouvrage.git.providers import detect_provider
        provider = await detect_provider(repo)

    project_id = arguments["id"]
    result = await db.create_project(
        id=project_id,
        repo=repo,
        working_dir=resolved,
        default_branch=arguments.get("default_branch", "main"),
        setup_command=arguments.get("setup_command"),
        teardown_command=arguments.get("teardown_command"),
        test_command=arguments.get("test_command"),
        env_overrides=arguments.get("env_overrides"),
        max_turns=arguments.get("max_turns"),
        max_wall_clock=arguments.get("max_wall_clock"),
        model=arguments.get("model"),
        review_model=arguments.get("review_model"),
        review_ignore_patterns=arguments.get("review_ignore_patterns"),
        auto_test=arguments.get("auto_test"),
        auto_review=arguments.get("auto_review"),
        auto_pr=arguments.get("auto_pr"),
        auto_merge=arguments.get("auto_merge"),
        state_definitions=arguments.get("state_definitions"),
        created_by=get_request_user_id(),
        github_pat_override=cred_encrypted,
        provider=provider,
        credential_override=cred_encrypted,
        credential_override_last4=cred_last4,
    )

    # Validate credential access synchronously so the response includes status
    result = await _run_project_validation(project_id, result)

    return result


async def _handle_get_project(arguments):
    result = await db.get_project(arguments["id"])
    return result if result else {"error": f"Project '{arguments['id']}' not found"}


async def _handle_update_project(arguments):
    project_id = arguments["id"]
    fields = {k: v for k, v in arguments.items() if k != "id"}
    fields.pop("claude_md_path", None)
    if not fields:
        return {"error": "No fields to update"}
    if "repo" in fields:
        fields["repo"] = normalize_repo_url(fields["repo"])
    # Encrypt PAT override if a new value was provided; empty/null clears it
    if "github_pat_override" in fields:
        pat = fields["github_pat_override"]
        if pat:  # non-empty string → encrypt
            fields["github_pat_override"] = encrypt_value(pat) if not is_fernet_token(pat) else pat
        else:  # empty string or null → clear (db.update_project treats empty string as NULL)
            fields["github_pat_override"] = "" if pat == "" else None
    # credential_override follows same pattern
    if "credential_override" in fields:
        cred = fields["credential_override"]
        if cred:
            fields["credential_override_last4"] = cred[-4:] if len(cred) >= 4 else cred
            fields["credential_override"] = encrypt_value(cred) if not is_fernet_token(cred) else cred
        else:
            fields["credential_override"] = "" if cred == "" else None
            fields["credential_override_last4"] = None
    result = await db.update_project(project_id, **fields)

    # Re-validate credential if repo, provider, or credential changed
    revalidate_fields = {"repo", "provider", "credential_override", "github_pat_override"}
    if revalidate_fields & fields.keys():
        result = await _run_project_validation(project_id, result)

    return result


async def _handle_pause_project(arguments):
    return await task_engine.pause_project(arguments["project_id"])


async def _handle_resume_project(arguments):
    return await task_engine.resume_project(arguments["project_id"])


async def _handle_stop_project(arguments):
    return await task_engine.stop_project(arguments["project_id"])


async def _handle_list_projects(arguments):
    return await db.list_projects()


async def _handle_delete_project(arguments):
    """Delete a project and remove its working directory from disk.

    Rejects if the project has tasks in 'working' status.
    """
    project_id = arguments["project_id"]

    project = await db.get_project(project_id)
    if not project:
        return {"error": f"Project '{project_id}' not found"}

    # Check for active tasks (working or validating)
    active_tasks = []
    for active_status in ("working", "validating"):
        active_tasks.extend(await db.list_tasks(project_id=project_id, status=active_status))
    if active_tasks:
        task_ids = [t["id"] for t in active_tasks]
        return {
            "error": f"Cannot delete project '{project_id}' — {len(active_tasks)} task(s) are still working or validating: {', '.join(task_ids)}. "
                     "Cancel or wait for them to finish first."
        }

    working_dir = project.get("working_dir")

    # Delete project row from DB
    await db.delete_project(project_id)

    # Remove working directory from disk (bare repo + worktrees)
    if working_dir and os.path.isdir(working_dir):
        try:
            shutil.rmtree(working_dir)
            log.info(f"Removed working directory for deleted project '{project_id}': {working_dir}")
        except Exception as e:
            log.warning(f"Failed to remove working directory '{working_dir}' for project '{project_id}': {e}")
            return {
                "deleted": True,
                "project_id": project_id,
                "warning": f"Project row deleted but failed to remove working directory: {e}",
            }

    return {"deleted": True, "project_id": project_id, "working_dir": working_dir}
