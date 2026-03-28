"""Project tool handlers."""

import os

import switchboard.db as db
import switchboard.dispatch as task_engine
from switchboard.server.context import get_request_user_id
from switchboard.git.operations import normalize_repo_url

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


async def _handle_create_project(arguments):
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

    return await db.create_project(
        id=arguments["id"],
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
    )


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
    return await db.update_project(project_id, **fields)


async def _handle_pause_project(arguments):
    return await task_engine.pause_project(arguments["project_id"])


async def _handle_resume_project(arguments):
    return await task_engine.resume_project(arguments["project_id"])


async def _handle_stop_project(arguments):
    return await task_engine.stop_project(arguments["project_id"])


async def _handle_list_projects(arguments):
    return await db.list_projects()
