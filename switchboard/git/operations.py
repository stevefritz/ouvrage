"""Git branch operations — rebase, merge, push, PR creation, diff extraction."""

import asyncio
import logging
import os

import database as db
from switchboard.git.worktree import _run_as_worker

log = logging.getLogger("switchboard.tasks")


async def resolve_branch_target(task: dict) -> str:
    """Resolve the merge target branch using config inheritance.

    Priority: task.branch_target (explicit override) → task.base_branch
              → component.base_branch → project.default_branch

    NOTE: depends_on has ZERO influence on merge target. It controls dispatch
    ordering and worktree base (setup_worktree), never where we merge to.
    """
    # 1. Task-level override
    if task.get("base_branch"):
        return task["base_branch"]

    # 3. Component-level
    if task.get("component_id"):
        component = await db.get_component(task["component_id"])
        if component and component.get("base_branch"):
            return component["base_branch"]

    # 4. Project default
    project = await db.get_project(task["project_id"])
    return project["default_branch"] if project else "main"


async def _ensure_branch_pushed(task_id: str, task: dict) -> None:
    """Force-push task branch if there are unpushed commits."""
    worktree = task.get("worktree_path")
    branch = task.get("branch")
    if not worktree or not branch or not os.path.exists(worktree):
        return

    # Check if remote branch exists
    stdout, _, rc = await _run_as_worker(
        "git", "-C", worktree, "ls-remote", "--heads", "origin", branch,
    )
    remote_exists = rc == 0 and stdout.strip()

    if remote_exists:
        # Check for unpushed commits
        stdout, _, rc = await _run_as_worker(
            "git", "-C", worktree, "log", f"origin/{branch}..HEAD", "--oneline",
        )
        if rc != 0 or not stdout.strip():
            return  # nothing to push
    # else: remote doesn't exist yet, push to create it

    _, stderr, rc = await _run_as_worker(
        "git", "-C", worktree, "push", "--force-with-lease", "origin", branch,
    )
    if rc != 0:
        log.warning(f"Auto-push failed for {task_id}: {stderr.decode()}")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Auto-push failed",
            content=f"```\n{stderr.decode()[:1000]}\n```",
        )
    else:
        log.info(f"Auto-pushed branch {branch} for {task_id}")


async def _git_fetch_and_rebase(worktree: str, target_branch: str) -> bool:
    """Fetch origin and rebase worktree onto origin/{target_branch}.

    Returns True on success. Returns False if rebase failed (rebase is aborted
    before returning).
    """
    await _run_as_worker("git", "-C", worktree, "fetch", "origin")

    _, _stderr, rc = await _run_as_worker(
        "git", "-C", worktree, "rebase", f"origin/{target_branch}",
    )

    if rc != 0:
        await _run_as_worker("git", "-C", worktree, "rebase", "--abort")
        return False

    return True


async def _sync_branch_with_base(task: dict) -> bool:
    """Fetch origin and rebase task worktree onto the task's base branch.

    Returns True if rebase succeeded (or no worktree to sync).
    Returns False if rebase failed — in that case gate_status is set to 'needs-review'
    and an error message is posted to the task thread.
    """
    worktree = task.get("worktree_path")
    if not worktree or not os.path.exists(worktree):
        return True  # Nothing to sync

    base_branch = await resolve_branch_target(task)
    success = await _git_fetch_and_rebase(worktree, base_branch)

    if not success:
        log.warning(f"Rebase failed for {task['id']} onto origin/{base_branch}")
        await db.update_task(task["id"], gate_status="needs-review")
        await db.post_task_message(
            task_id=task["id"], author="switchboard", type="status",
            title="Rebase conflict — needs review",
            content=(
                f"Automatic rebase onto `origin/{base_branch}` failed due to conflicts.\n\n"
                f"Resolve manually in the worktree:\n"
                f"```\ncd {worktree}\ngit rebase origin/{base_branch}\n"
                f"# resolve conflicts, then:\ngit rebase --continue\n```"
            ),
        )
        return False

    log.info(f"Rebased {task['id']} onto origin/{base_branch} successfully")
    return True


async def _get_branch_diff(task: dict) -> str:
    """Get git diff between default branch and task branch."""
    worktree = task.get("worktree_path")
    if not worktree or not os.path.exists(worktree):
        return "(no worktree available)"
    project = await db.get_project(task["project_id"])
    default_branch = project["default_branch"] if project else "main"
    stdout, stderr, rc = await _run_as_worker(
        "git", "-C", worktree, "diff", f"origin/{default_branch}...HEAD", "--stat",
    )
    stat = stdout.decode(errors="replace")
    stdout2, _, _ = await _run_as_worker(
        "git", "-C", worktree, "diff", f"origin/{default_branch}...HEAD",
    )
    full_diff = stdout2.decode(errors="replace")
    return f"{stat}\n\n{full_diff}"


def _filter_diff_by_ignore_patterns(diff: str, patterns: list[str]) -> str:
    """Strip file sections from a unified diff whose paths match any ignore pattern."""
    if not patterns:
        return diff

    lines = diff.splitlines(keepends=True)
    result = []
    skip_section = False

    for line in lines:
        # New file section: lines starting with "diff --git"
        if line.startswith("diff --git "):
            # Check if this file path matches any ignore pattern
            skip_section = any(pat in line for pat in patterns)
        if not skip_section:
            result.append(line)

    return "".join(result)


async def _maybe_create_pr(task_id: str) -> None:
    """Create PR if auto_pr is enabled and this is the tail of a chain."""
    task = await db.get_task(task_id)
    if not task or not task.get("auto_pr"):
        return

    # Check no dependents are waiting
    dependents = await db.get_dependents(task_id)
    if any(d["status"] not in ("completed", "cancelled") for d in dependents):
        return  # Not the tail yet

    project = await db.get_project(task["project_id"])
    if not project:
        return

    worktree = task.get("worktree_path")
    branch = task.get("branch")
    default_branch = project["default_branch"]

    if not worktree or not branch:
        return

    # Walk the chain to collect goals
    chain = await db.get_chain(task_id)
    goals = [t["goal"] for t in chain if not t.get("parent_task_id")]  # Exclude review tasks

    title = task["goal"][:70]
    body = "## Summary\n" + "\n".join(f"- {g}" for g in goals)

    log.info(f"Auto-creating PR for {task_id}: {branch} → {default_branch}")
    stdout, stderr, rc = await _run_as_worker(
        "gh", "pr", "create",
        "--title", title,
        "--body", body,
        "--base", default_branch,
        "--head", branch,
        cwd=worktree,
    )

    if rc == 0:
        pr_url = stdout.decode().strip()
        await db.add_artifact(task_id, "pr_url", pr_url)
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="PR Created",
            content=f"[{pr_url}]({pr_url})",
        )
        log.info(f"PR created for {task_id}: {pr_url}")
    else:
        log.warning(f"PR creation failed for {task_id}: {stderr.decode()}")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="PR creation failed",
            content=f"```\n{stderr.decode()[:2000]}\n```",
        )


async def _perform_auto_merge(task_id: str) -> bool:
    """Merge task branch into branch_target using detached HEAD. Returns True on success.

    Uses `git checkout --detach origin/<target>` instead of checking out the target
    branch by name, which avoids the "fatal: a branch named 'X' already exists"
    error that occurs when the target branch is checked out in another worktree.
    """
    task = await db.get_task(task_id)
    if not task:
        return False

    branch_target = await resolve_branch_target(task)
    await db.update_task(task_id, branch_target=branch_target)

    worktree = task.get("worktree_path")
    task_branch = task.get("branch")
    if not worktree or not task_branch:
        log.error(f"Auto-merge {task_id}: missing worktree or branch")
        await db.update_task(task_id, pr_status="error", pr_error="Missing worktree or branch")
        return False

    try:
        for attempt in range(1, 4):
            # Fetch latest target branch
            await _run_as_worker("git", "-C", worktree, "fetch", "origin", branch_target)

            # Detach HEAD at origin/branch_target — no local branch name conflict possible
            _, stderr, rc = await _run_as_worker(
                "git", "-C", worktree, "checkout", "--detach", f"origin/{branch_target}",
            )
            if rc != 0:
                log.error(f"Auto-merge {task_id}: cannot detach to origin/{branch_target}: {stderr.decode()}")
                await db.update_task(task_id, status="needs-review",
                                     pr_status="error", pr_error=f"Cannot checkout origin/{branch_target}")
                await db.post_task_message(
                    task_id=task_id, author="dispatcher", type="status",
                    title="Auto-merge failed",
                    content=f"Cannot detach HEAD at `origin/{branch_target}`:\n```\n{stderr.decode()[:1000]}\n```",
                )
                return False

            # Merge the task branch
            stdout, stderr, rc = await _run_as_worker(
                "git", "-C", worktree, "merge", task_branch, "--no-edit",
            )

            if rc != 0:
                # Merge conflict — get list of conflicting files
                conflict_stdout, _, _ = await _run_as_worker(
                    "git", "-C", worktree, "diff", "--name-only", "--diff-filter=U",
                )
                conflict_files = conflict_stdout.decode().strip()

                # Abort the merge
                await _run_as_worker("git", "-C", worktree, "merge", "--abort")

                await db.update_task(task_id, status="needs-review",
                                     pr_status="conflict", pr_error=conflict_files)
                await db.post_task_message(
                    task_id=task_id, author="dispatcher", type="status",
                    title="Auto-merge conflict",
                    content=f"Merge of `{task_branch}` into `{branch_target}` has conflicts:\n\n"
                            f"```\n{conflict_files or '(unknown)'}\n```\n\n"
                            f"Resolve manually and retry.",
                )
                log.warning(f"Auto-merge {task_id}: conflict merging {task_branch} → {branch_target}")
                return False

            # Push HEAD to target branch — HEAD:branch_target avoids needing a local branch
            _, stderr, rc = await _run_as_worker(
                "git", "-C", worktree, "push", "origin", f"HEAD:{branch_target}",
            )
            if rc == 0:
                break  # push succeeded

            # Push rejected — likely a race with another task; retry
            log.warning(f"Auto-merge {task_id}: push attempt {attempt} rejected, retrying...")
            if attempt == 3:
                log.error(f"Auto-merge {task_id}: push failed after 3 attempts: {stderr.decode()}")
                await db.update_task(task_id, status="needs-review",
                                     pr_status="push-failed", pr_error=stderr.decode()[:500])
                await db.post_task_message(
                    task_id=task_id, author="dispatcher", type="status",
                    title="Auto-merge push failed",
                    content=f"Merge succeeded but push to `{branch_target}` failed after 3 attempts:\n```\n{stderr.decode()[:1000]}\n```",
                )
                return False

        await db.update_task(task_id, status="merged", pushed_at=db.now_iso(), pr_status="merged")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Auto-merged",
            content=f"Branch `{task_branch}` merged into `{branch_target}` and pushed.",
        )
        log.info(f"Auto-merge {task_id}: {task_branch} → {branch_target} success")
        return True

    finally:
        # Always restore worktree to original task branch
        await _run_as_worker("git", "-C", worktree, "checkout", task_branch)
