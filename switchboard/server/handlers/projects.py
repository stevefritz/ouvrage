"""Project tool handlers."""

import asyncio
import logging
import os
import shutil

import switchboard.db as db
import switchboard.dispatch as task_engine
from switchboard.server.context import get_request_user_id
from switchboard.git.operations import normalize_repo_url, _build_authenticated_url
from switchboard.git.worktree import _run_as_worker
from switchboard.crypto import encrypt_value, is_fernet_token
from switchboard.config.settings import SKIP_CREDENTIAL_CHECK

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


async def _validate_github_pat_for_repo(repo: str) -> dict | None:
    """Check that a GitHub PAT is configured and can access the given repo.

    Returns None if valid, or {"error": "..."} if not.
    Runs git ls-remote without writing any files.
    """
    try:
        pat = await db.get_instance_github_pat()
    except ValueError:
        return {"error": "Add your GitHub PAT in Settings before creating projects."}

    if not pat:
        return {"error": "Add your GitHub PAT in Settings before creating projects."}

    # Test PAT access to the repo using git ls-remote (read-only, no file writes)
    auth_url = _build_authenticated_url(pat, repo)
    try:
        stdout, stderr, rc = await asyncio.wait_for(
            _run_as_worker("git", "ls-remote", "--exit-code", auth_url, "HEAD"),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        return {"error": "GitHub PAT cannot access this repo. Check your token's permissions."}
    except Exception:
        return {"error": "GitHub PAT cannot access this repo. Check your token's permissions."}

    if rc != 0:
        return {"error": "GitHub PAT cannot access this repo. Check your token's permissions."}

    return None


async def _handle_create_project(arguments):
    max_projects = await db.get_max_projects()
    if max_projects > 0:
        current_count = await db.count_projects()
        if current_count >= max_projects:
            return {"error": f"Project limit reached ({current_count}/{max_projects}). Upgrade your plan for more projects."}

    repo = normalize_repo_url(arguments["repo"])
    working_dir = arguments.get("working_dir") or _resolve_working_dir(
        repo, arguments.get("folder_name")
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

    pat_raw = arguments.get("github_pat_override")
    pat_encrypted = encrypt_value(pat_raw) if pat_raw and not is_fernet_token(pat_raw) else pat_raw or None

    # 2a + 2b: validate PAT exists and can access the repo before creating any DB row.
    # When SKIP_CREDENTIAL_CHECK=true, skip the PAT-exists check but still validate
    # repo access if a PAT IS configured (don't bypass clone validation when a key is present).
    if SKIP_CREDENTIAL_CHECK:
        try:
            instance_pat = await db.get_instance_github_pat()
        except ValueError:
            instance_pat = None
        if instance_pat:
            pat_error = await _validate_github_pat_for_repo(repo)
            if pat_error:
                return pat_error
    else:
        pat_error = await _validate_github_pat_for_repo(repo)
        if pat_error:
            return pat_error

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
        github_pat_override=pat_encrypted,
    )
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
    return await db.update_project(project_id, **fields)


async def _handle_pause_project(arguments):
    return await task_engine.pause_project(arguments["project_id"])


async def _handle_resume_project(arguments):
    return await task_engine.resume_project(arguments["project_id"])


async def _handle_stop_project(arguments):
    return await task_engine.stop_project(arguments["project_id"])


async def _handle_list_projects(arguments):
    return await db.list_projects()


async def _handle_rename_project(arguments):
    """Rename a project: cascade ID change through all DB tables and rename working dir."""
    project_id = arguments["project_id"]
    new_id = arguments["new_id"]

    try:
        project = await db.rename_project(project_id, new_id)
    except ValueError as e:
        return {"error": str(e)}

    # Rename working directory on disk (best-effort; DB is already committed)
    old_working_dir = None
    # Reconstruct old working dir: same parent, old folder name
    new_working_dir = project.get("working_dir")
    if new_working_dir:
        parent = os.path.dirname(new_working_dir)
        old_working_dir = os.path.join(parent, project_id)

    if old_working_dir and os.path.isdir(old_working_dir):
        try:
            os.rename(old_working_dir, new_working_dir)
            log.info(
                "Renamed working directory for project '%s' → '%s': %s → %s",
                project_id, new_id, old_working_dir, new_working_dir,
            )
        except Exception as e:
            log.warning(
                "Failed to rename working directory '%s' → '%s' for project rename '%s' → '%s': %s",
                old_working_dir, new_working_dir, project_id, new_id, e,
            )
            return {
                **project,
                "warning": (
                    f"Project renamed in DB but failed to rename working directory "
                    f"'{old_working_dir}' → '{new_working_dir}': {e}"
                ),
            }

    return {**project, "renamed": True, "old_id": project_id}


async def _handle_delete_project(arguments):
    """Delete a project and remove its working directory from disk.

    Rejects if the project has tasks in 'working' status.
    """
    project_id = arguments["project_id"]

    project = await db.get_project(project_id)
    if not project:
        return {"error": f"Project '{project_id}' not found"}

    # Check for active (working) tasks
    working_tasks = await db.list_tasks(project_id=project_id, status="working")
    if working_tasks:
        task_ids = [t["id"] for t in working_tasks]
        return {
            "error": f"Cannot delete project '{project_id}' — {len(working_tasks)} task(s) are still working: {', '.join(task_ids)}. "
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
