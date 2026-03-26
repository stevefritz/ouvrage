"""Git file access — list and read files from task branches/worktrees."""

import asyncio
import logging
import os
import time

import switchboard.db as db
from switchboard.git.worktree import _run_as_worker

log = logging.getLogger("switchboard.server")

# TTL cache for git fetch — maps bare_path -> last fetch monotonic time.
# Prevents a fetch on every single call for released/merged tasks.
_fetch_cache: dict[str, float] = {}
_FETCH_TTL: float = 60.0  # seconds


async def _git_run(args: list[str], git_dir: str, timeout: float = 30.0) -> tuple[bytes, int]:
    """Run a git command with -C git_dir as the worker user. Returns (stdout_bytes, returncode).

    Runs as the worker user (via _run_as_worker) so that bare repos owned by that user
    pass git's safe-directory ownership check.

    Raises asyncio.TimeoutError if the command exceeds timeout seconds.
    """
    stdout, _, rc = await asyncio.wait_for(
        _run_as_worker("git", "-C", git_dir, *args),
        timeout=timeout,
    )
    return stdout, rc


async def _resolve_git_ref(task: dict, project: dict) -> tuple[str, str] | None:
    """Return (git_dir, ref) for accessing task files, or None if inaccessible.

    Resolution order:
    1. Active worktree on disk → (worktree_path, "HEAD")
    2. Branch on origin (released/merged) → (bare_path, "origin/{branch}")
    3. Inaccessible → None
    """
    worktree_path = task.get("worktree_path")
    branch = task.get("branch")
    bare_path = os.path.join(project["working_dir"], ".bare")

    # Priority 1: active worktree exists on disk
    if worktree_path and os.path.isdir(worktree_path):
        return (worktree_path, "HEAD")

    # Priority 2: branch on origin
    if branch:
        now = time.monotonic()
        if now - _fetch_cache.get(bare_path, 0.0) > _FETCH_TTL:
            _, fetch_rc = await _git_run(["fetch", "origin", "--prune", "-q"], bare_path)
            if fetch_rc == 0:
                _fetch_cache[bare_path] = now
        _, rc = await _git_run(
            ["rev-parse", "--verify", f"origin/{branch}"],
            bare_path,
        )
        if rc == 0:
            return (bare_path, f"origin/{branch}")

    return None


def _is_binary(data: bytes) -> bool:
    """Detect binary content by checking for null bytes in first 8KB."""
    return b"\x00" in data[:8192]


def _validate_path(path: str) -> str | None:
    """Return an error string if path is unsafe, else None."""
    if ".." in path.split("/"):
        return "Path components must not contain '..'"
    return None


async def _handle_list_task_files(arguments: dict):
    task_id = arguments["task_id"]
    path = arguments.get("path", "").strip("/")
    recursive = arguments.get("recursive", False)

    task = await db.get_task(task_id)
    if not task:
        return {"error": f"Task not found: {task_id}"}

    project = await db.get_project(task["project_id"])
    if not project:
        return {"error": f"Project not found: {task['project_id']}"}

    if path:
        err = _validate_path(path)
        if err:
            return {"error": err}

    resolved = await _resolve_git_ref(task, project)
    if not resolved:
        return {
            "error": "Task files are not accessible",
            "detail": (
                "No worktree on disk and no branch on origin. "
                "The branch may have been deleted or never pushed."
            ),
            "task_id": task_id,
            "status": task.get("status"),
        }

    git_dir, ref = resolved

    # Build ls-tree command
    cmd = ["ls-tree", "--name-only"]
    if recursive:
        cmd.append("-r")
    cmd.append(ref)
    if path:
        cmd.extend(["--", f"{path}/"])

    stdout, rc = await _git_run(cmd, git_dir)
    if rc != 0:
        if path:
            return {"error": f"Path not found in task branch: {path!r}"}
        return {"error": "git ls-tree failed", "returncode": rc}

    files = [f for f in stdout.decode("utf-8", errors="replace").splitlines() if f]
    return {
        "task_id": task_id,
        "path": path or "/",
        "recursive": recursive,
        "files": files,
        "count": len(files),
        "ref_used": ref,
    }


async def _handle_get_task_file(arguments: dict):
    task_id = arguments["task_id"]
    path = arguments["path"].strip("/")
    max_bytes = arguments.get("max_bytes", 1048576)  # 1MB default

    task = await db.get_task(task_id)
    if not task:
        return {"error": f"Task not found: {task_id}"}

    project = await db.get_project(task["project_id"])
    if not project:
        return {"error": f"Project not found: {task['project_id']}"}

    err = _validate_path(path)
    if err:
        return {"error": err}

    resolved = await _resolve_git_ref(task, project)
    if not resolved:
        return {
            "error": "Task files are not accessible",
            "detail": (
                "No worktree on disk and no branch on origin. "
                "The branch may have been deleted or never pushed."
            ),
            "task_id": task_id,
            "status": task.get("status"),
        }

    git_dir, ref = resolved

    # Check object type — reject directories with a clear message
    type_out, type_rc = await _git_run(["cat-file", "-t", f"{ref}:{path}"], git_dir)
    if type_rc != 0:
        return {"error": f"File not found in task branch: {path!r}", "ref": ref}
    if type_out.decode().strip() == "tree":
        return {
            "error": "Path is a directory, not a file. Use list_task_files instead.",
            "path": path,
        }

    stdout, rc = await _git_run(["show", f"{ref}:{path}"], git_dir)
    if rc != 0:
        return {"error": f"File not found in task branch: {path!r}", "ref": ref}

    size = len(stdout)

    # Binary detection on first 8KB
    if _is_binary(stdout[:8192]):
        return {
            "error": "File is binary and cannot be returned as text",
            "path": path,
            "size": size,
            "binary": True,
            "ref_used": ref,
        }

    truncated = size > max_bytes
    content = stdout[:max_bytes].decode("utf-8", errors="replace")

    return {
        "task_id": task_id,
        "path": path,
        "content": content,
        "size": size,
        "binary": False,
        "truncated": truncated,
        "ref_used": ref,
    }
