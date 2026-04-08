"""Git push/fetch tool handlers — worker-only MCP tools.

These tools handle all remote git operations server-side so that
CC workers never see credentials. The PAT is resolved in memory,
used for a single git command, and falls out of scope immediately.
"""

import logging
import os

import switchboard.db as db
from switchboard.git.providers import resolve_credential
from switchboard.git.worktree import _run_as_worker

log = logging.getLogger("switchboard.server")


async def _handle_git_push(arguments: dict) -> dict:
    """Push the task's branch to origin using server-side credentials."""
    task_id = arguments["task_id"]

    task = await db.get_task(task_id)
    if not task:
        return {"pushed": False, "error": "not_found", "message": f"Task '{task_id}' not found"}

    worktree = task.get("worktree_path")
    branch = task.get("branch")
    if not worktree or not branch:
        return {"pushed": False, "error": "no_worktree", "message": "Task has no worktree or branch"}

    if not os.path.exists(worktree):
        return {"pushed": False, "error": "no_worktree", "message": f"Worktree does not exist: {worktree}"}

    # Scope safety: verify the current branch matches the task branch
    stdout, _, rc = await _run_as_worker(
        "git", "-C", worktree, "rev-parse", "--abbrev-ref", "HEAD",
    )
    current_branch = stdout.decode().strip()
    if current_branch != branch:
        return {
            "pushed": False,
            "error": "wrong_branch",
            "message": f"Current branch '{current_branch}' does not match task branch '{branch}'. "
                       f"Only the task's own branch can be pushed.",
        }

    # Check for unpushed commits
    stdout, _, rc = await _run_as_worker(
        "git", "-C", worktree, "log", f"origin/{branch}..HEAD", "--oneline",
    )
    if rc != 0:
        # origin/branch may not exist yet — that means everything is unpushed
        # Check if there are any commits at all
        stdout_check, _, rc_check = await _run_as_worker(
            "git", "-C", worktree, "log", "--oneline", "-1",
        )
        if rc_check != 0 or not stdout_check.strip():
            return {"pushed": False, "message": "Nothing to push — no commits"}
        commit_lines = stdout_check.decode().strip().splitlines()
        num_commits = len(commit_lines)
    else:
        if not stdout.strip():
            return {"pushed": False, "message": "Nothing to push — no new commits"}
        commit_lines = stdout.decode().strip().splitlines()
        num_commits = len(commit_lines)

    project = await db.get_project(task["project_id"])
    if not project:
        return {"pushed": False, "error": "no_project", "message": f"Project '{task['project_id']}' not found"}

    # Resolve credential and build authenticated URL via provider interface
    try:
        provider, credential = await resolve_credential(project)
    except ValueError as e:
        return {"pushed": False, "error": "no_credential", "message": str(e)}

    auth_url = provider.build_authenticated_url(project["repo"], credential)

    # Push
    _, stderr, rc = await _run_as_worker(
        "git", "-C", worktree, "push", auth_url, branch,
    )

    if rc == 0:
        # Update tracking ref directly so future `git log origin/branch..HEAD` works.
        # We can't use `git fetch origin` here because the remote URL is unauthenticated
        # and would fail on private repos. Instead, update the ref in the bare repo directly.
        # Worktrees share refs with the bare repo, so this is immediately visible.
        bare_path = os.path.join(project["working_dir"], ".bare")
        head_stdout, _, head_rc = await _run_as_worker(
                "git", "-C", worktree, "rev-parse", "HEAD",
            )
        if head_rc == 0:
            head_sha = head_stdout.decode().strip()
            await _run_as_worker(
                "git", "-C", bare_path, "update-ref",
                f"refs/remotes/origin/{branch}", head_sha,
            )
        return {"pushed": True, "branch": branch, "commits": num_commits}

    stderr_text = stderr.decode()
    # Detect divergence / push rejection
    s = stderr_text.lower()
    if "rejected" in s or "non-fast-forward" in s or "fetch first" in s:
        return {
            "pushed": False,
            "error": "push_rejected",
            "message": "Push rejected — remote has diverged. Call git_fetch first, "
                       "then merge and resolve conflicts locally, then call git_push again.",
        }

    return {
        "pushed": False,
        "error": "push_failed",
        "message": f"Push failed: {stderr_text[:1000]}",
    }


async def _handle_git_fetch(arguments: dict) -> dict:
    """Fetch from remote into the task's worktree using server-side credentials."""
    task_id = arguments["task_id"]
    ref = arguments.get("ref")

    task = await db.get_task(task_id)
    if not task:
        return {"fetched": False, "error": "not_found", "message": f"Task '{task_id}' not found"}

    worktree = task.get("worktree_path")
    if not worktree:
        return {"fetched": False, "error": "no_worktree", "message": "Task has no worktree"}

    if not os.path.exists(worktree):
        return {"fetched": False, "error": "no_worktree", "message": f"Worktree does not exist: {worktree}"}

    project = await db.get_project(task["project_id"])
    if not project:
        return {"fetched": False, "error": "no_project", "message": f"Project '{task['project_id']}' not found"}

    # Resolve bare repo path
    bare_path = os.path.join(project["working_dir"], ".bare")

    # Resolve credential and build authenticated URL via provider interface
    try:
        provider, credential = await resolve_credential(project)
    except ValueError as e:
        return {"fetched": False, "error": "no_credential", "message": str(e)}

    auth_url = provider.build_authenticated_url(project["repo"], credential)

    if ref:
        # Fetch specific branch into bare repo
        _, stderr, rc = await _run_as_worker(
            "git", "-C", bare_path, "fetch", auth_url,
            f"+refs/heads/{ref}:refs/remotes/origin/{ref}",
        )
        if rc != 0:
            return {"fetched": False, "error": "fetch_failed",
                    "message": f"Fetch failed: {stderr.decode()[:1000]}"}

        # No need to fetch in the worktree — worktrees share refs/remotes/origin/*
        # with the bare repo, so the refs fetched above are immediately visible.
        return {"fetched": True, "ref": ref}
    else:
        # Fetch all into bare repo
        _, stderr, rc = await _run_as_worker(
            "git", "-C", bare_path, "fetch", auth_url,
            "+refs/heads/*:refs/remotes/origin/*",
        )
        if rc != 0:
            return {"fetched": False, "error": "fetch_failed",
                    "message": f"Fetch failed: {stderr.decode()[:1000]}"}

        # No need to fetch in the worktree — worktrees share refs/remotes/origin/*
        # with the bare repo, so the refs fetched above are immediately visible.
        return {"fetched": True, "ref": "all"}
