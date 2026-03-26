"""Project tool handlers."""

import os

import database as db
import tasks as task_engine

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
    working_dir = arguments.get("working_dir") or _resolve_working_dir(
        arguments["repo"], arguments.get("folder_name")
    )
    # Enforce worktree base — no escaping
    resolved = os.path.realpath(working_dir)
    base = os.path.realpath(WORKTREE_BASE)
    if not resolved.startswith(base + "/") and resolved != base:
        raise ValueError(f"working_dir must be under {WORKTREE_BASE}, got: {working_dir}")

    return await db.create_project(
        id=arguments["id"],
        repo=arguments["repo"],
        working_dir=resolved,
        default_branch=arguments.get("default_branch", "main"),
        setup_command=arguments.get("setup_command"),
        teardown_command=arguments.get("teardown_command"),
        test_command=arguments.get("test_command"),
        env_overrides=arguments.get("env_overrides"),
        max_turns=arguments.get("max_turns"),
        max_wall_clock=arguments.get("max_wall_clock"),
        claude_md_path=arguments.get("claude_md_path"),
        model=arguments.get("model"),
        state_definitions=arguments.get("state_definitions"),
    )


async def _handle_get_project(arguments):
    result = await db.get_project(arguments["id"])
    return result if result else {"error": f"Project '{arguments['id']}' not found"}


async def _handle_update_project(arguments):
    project_id = arguments["id"]
    fields = {k: v for k, v in arguments.items() if k != "id"}
    if not fields:
        return {"error": "No fields to update"}
    return await db.update_project(project_id, **fields)


async def _handle_pause_project(arguments):
    return await task_engine.pause_project(arguments["project_id"])


async def _handle_resume_project(arguments):
    return await task_engine.resume_project(arguments["project_id"])


async def _handle_stop_project(arguments):
    return await task_engine.stop_project(arguments["project_id"])


async def _handle_list_projects(arguments):
    return await db.list_projects()
