"""Git worktree management — setup, teardown, and worker-user primitives."""

import asyncio
import json
import logging
import os
import pwd
import shlex

import switchboard.db as db
from switchboard.config.settings import WORKER_USER

log = logging.getLogger("switchboard.tasks")


def _get_worker_ids() -> tuple[int, int]:
    """Get uid/gid for the worker user."""
    pw = pwd.getpwnam(WORKER_USER)
    return pw.pw_uid, pw.pw_gid


async def _run_as_worker(*cmd, **kwargs) -> tuple[bytes, bytes, int]:
    """Run a command as the worker user via setuid (requires CAP_SETUID)."""
    uid, gid = _get_worker_ids()
    pw = pwd.getpwnam(WORKER_USER)

    def _demote():
        os.setgid(gid)
        os.setuid(uid)

    # Ensure HOME is set to the worker user's home dir, not the service user's
    env = kwargs.pop("env", None) or os.environ.copy()
    env["HOME"] = pw.pw_dir

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        preexec_fn=_demote,
        env=env,
        **kwargs,
    )
    stdout, stderr = await proc.communicate()
    return stdout, stderr, proc.returncode


async def _find_branch_holder(branch: str) -> dict | None:
    """Find a task that holds a worktree on the given branch."""
    async with db.get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT id, status, worktree_path FROM tasks WHERE branch = ? AND worktree_path IS NOT NULL",
            (branch,),
        )
        if rows:
            r = rows[0]
            return {"task_id": r["id"], "status": r["status"], "worktree_path": r["worktree_path"]}
    return None


async def setup_worktree(project: dict, dir_name: str, branch: str,
                         depends_on: str | None = None) -> str:
    """Create git worktree for a task. Returns worktree path.

    Args:
        project: Project config dict.
        dir_name: Filesystem-safe directory name (no slashes).
        branch: Git branch name (may contain slashes like feature/foo).
        depends_on: Parent task ID for branch chaining (branch from parent's branch).
    """
    base = project["working_dir"]
    worktree_path = os.path.join(base, dir_name)

    if os.path.exists(worktree_path):
        log.info(f"Worktree already exists: {worktree_path}, pulling latest")
        # Fetch + pull so resumed tasks pick up upstream changes
        await _run_as_worker("git", "-C", worktree_path, "fetch", "origin")
        stdout, stderr, rc = await _run_as_worker(
            "git", "-C", worktree_path, "merge", "--ff-only",
            f"origin/{branch}",
        )
        if rc != 0:
            # Non-fatal — branch may not exist on remote yet, or diverged
            log.info(f"Auto-pull skipped (ff-only failed): {stderr.decode().strip()}")
        return worktree_path

    # Ensure base directory exists (created as worker user so ownership is correct)
    await _run_as_worker("mkdir", "-p", base)

    # Clone the repo as a bare repo if the base doesn't have .git
    bare_path = os.path.join(base, ".bare")
    if not os.path.exists(bare_path):
        log.info(f"Cloning bare repo: {project['repo']} -> {bare_path}")
        stdout, stderr, rc = await _run_as_worker(
            "git", "clone", "--bare", project["repo"], bare_path,
        )
        if rc != 0:
            raise RuntimeError(f"git clone --bare failed: {stderr.decode()}")

    # Fetch latest from remote — all tracking refs updated (origin/main, etc.)
    _, fetch_err, fetch_rc = await _run_as_worker("git", "-C", bare_path, "fetch", "origin")
    if fetch_rc != 0:
        log.warning(f"git fetch origin failed (rc={fetch_rc}): {fetch_err.decode().strip()}")

    # Auto-detect default branch from bare clone HEAD if project config is wrong
    default_branch = project["default_branch"]
    stdout, _, _ = await _run_as_worker("git", "-C", bare_path, "symbolic-ref", "HEAD")
    detected = stdout.decode().strip().removeprefix("refs/heads/")
    if detected and detected != default_branch:
        log.info(f"Auto-detected default branch '{detected}' (project config said '{default_branch}')")
        default_branch = detected

    # Branch chaining: if this task depends on another, branch from parent's branch.
    # Use origin/ remote tracking ref (always current after fetch) instead of local
    # branch ref, which can go stale or fail to update when checked out in a worktree.
    base_branch = f"origin/{default_branch}"
    if depends_on:
        parent_task = await db.get_task(depends_on)
        if parent_task and parent_task.get("branch"):
            base_branch = parent_task["branch"]
            log.info(f"Branch chaining: branching from parent branch '{base_branch}' (depends_on={depends_on})")

    stdout, stderr, rc = await _run_as_worker(
        "git", "-C", bare_path, "worktree", "add",
        "-b", branch, worktree_path, base_branch,
    )
    if rc != 0:
        error_msg = stderr.decode()
        # Stale local branch ref — delete it and retry with -b (fresh branch from base)
        if "already exists" in error_msg:
            log.info(f"Deleting stale branch ref '{branch}' and retrying")
            await _run_as_worker("git", "-C", bare_path, "branch", "-D", branch)
            stdout, stderr, rc = await _run_as_worker(
                "git", "-C", bare_path, "worktree", "add",
                "-b", branch, worktree_path, base_branch,
            )
        # Branch exists on remote but not as stale ref — checkout existing
        if rc != 0:
            stdout, stderr, rc = await _run_as_worker(
                "git", "-C", bare_path, "worktree", "add",
                worktree_path, branch,
            )
        if rc != 0:
            error_msg = stderr.decode()
            # Check if branch is already checked out by another worktree
            if "already checked out" in error_msg or "is already used by worktree" in error_msg:
                blocking_info = await _find_branch_holder(branch)
                if blocking_info:
                    raise RuntimeError(
                        f"Cannot create worktree for branch '{branch}':\n"
                        f"  Branch held by task {blocking_info['task_id']}\n"
                        f"  Status: {blocking_info['status']} | Worktree: {blocking_info['worktree_path']}\n"
                        f"  Action: release_worktree('{blocking_info['task_id']}') to free it"
                    )
            raise RuntimeError(f"git worktree add failed: {error_msg}")

    log.info(f"Created worktree: {worktree_path} on branch {branch}")

    # Lock git author to the worker's global config — CC workers sometimes
    # override user.name/email in the repo config, which sticks for all future tasks.
    await _run_as_worker("git", "-C", worktree_path, "config", "--unset", "user.name")
    await _run_as_worker("git", "-C", worktree_path, "config", "--unset", "user.email")

    # Ensure the default branch and all remotes are visible from the worktree.
    # Bare-repo worktrees have a narrow fetch refspec by default, which makes
    # `git merge main` fail because CC can't resolve the branch.
    await _run_as_worker(
        "git", "-C", worktree_path, "config",
        "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*",
    )
    await _run_as_worker(
        "git", "-C", worktree_path, "fetch", "origin",
        default_branch + ":" + default_branch,
    )

    return worktree_path


async def run_setup_command(project: dict, worktree_path: str, env_overrides: dict | None = None):
    """Run project setup command in the worktree."""
    cmd = project.get("setup_command")
    if not cmd:
        return

    log.info(f"Running setup: {cmd} in {worktree_path}")
    stdout, stderr, rc = await _run_as_worker("sh", "-c", f"cd {shlex.quote(worktree_path)} && {cmd}")
    if rc != 0:
        log.warning(f"Setup command failed (exit {rc}): {stderr.decode()}")

    # Append env overrides AFTER setup (setup may create .env.testing from template)
    # Uses >> to append so we don't clobber APP_KEY etc. set by key:generate
    overrides = env_overrides
    if not overrides and project.get("env_overrides"):
        overrides = project["env_overrides"]
        if isinstance(overrides, str):
            overrides = json.loads(overrides)

    if overrides:
        env_path = os.path.join(worktree_path, ".env.testing")
        env_content = "\n" + "\n".join(f"{k}={v}" for k, v in overrides.items()) + "\n"
        # Append as worker user — later values override earlier ones in dotenv
        await _run_as_worker(
            "sh", "-c", f"cat >> {shlex.quote(env_path)} << 'ENVEOF'\n{env_content}ENVEOF"
        )
        log.info(f"Appended env overrides to {env_path}")


async def cleanup_worktree(project: dict, task: dict, force_delete_branch: bool = False):
    """Remove worktree and optionally delete branch."""
    worktree_path = task.get("worktree_path")
    bare_path = os.path.join(project["working_dir"], ".bare")

    # Run teardown command
    teardown = project.get("teardown_command")
    if teardown and worktree_path and os.path.exists(worktree_path):
        log.info(f"Running teardown: {teardown}")
        proc = await asyncio.create_subprocess_shell(
            teardown, cwd=worktree_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    # Remove worktree
    if worktree_path and os.path.exists(worktree_path):
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", bare_path, "worktree", "remove", "--force", worktree_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.warning(f"worktree remove failed: {stderr.decode()}")
        else:
            log.info(f"Removed worktree: {worktree_path}")

    # Delete branch
    branch = task.get("branch")
    if branch and os.path.exists(bare_path):
        flag = "-D" if force_delete_branch else "-d"
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", bare_path, "branch", flag, branch,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            log.info(f"Deleted branch: {branch}")
        else:
            log.info(f"Branch delete skipped (not merged or not found): {branch}")
