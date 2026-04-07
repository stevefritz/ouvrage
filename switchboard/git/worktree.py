"""Git worktree management — setup, teardown, and worker-user primitives."""

import asyncio
import hashlib
import json
import logging
import os
import pwd
import shlex
import shutil

import switchboard.db as db
from switchboard.config.settings import WORKER_USER
from switchboard.db.users import get_github_pat

log = logging.getLogger(__name__)


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


async def _seed_empty_repo(bare_path: str, project_id: str, default_branch: str, auth_url: str | None) -> None:
    """If the bare repo has no commits, create an initial commit and push to origin.

    Empty repos cause `git worktree add` to fail because there is no base ref
    to branch from. This seeds a minimal README.md commit so the first task
    dispatch can create a worktree successfully.
    """
    _, _, rc = await _run_as_worker("git", "-C", bare_path, "rev-parse", "HEAD")
    if rc == 0:
        return  # repo has commits — nothing to do

    log.info(f"Empty repo detected at {bare_path} — seeding initial commit for project '{project_id}'")

    stdout, _, rc = await _run_as_worker("mktemp", "-d", "/tmp/ouvrage-seed-XXXXXXXX")
    if rc != 0:
        raise RuntimeError("Failed to create temp dir for repo seeding")
    tmp_dir = stdout.decode().strip()
    try:
        # Clone the bare repo into a temp working tree
        _, stderr, rc = await _run_as_worker("git", "clone", bare_path, tmp_dir)
        if rc != 0:
            raise RuntimeError(f"git clone (seed) failed: {stderr.decode()}")

        # Set minimal git identity so the commit doesn't fail without global config
        await _run_as_worker("git", "-C", tmp_dir, "config", "user.email", "ouvrage@localhost")
        await _run_as_worker("git", "-C", tmp_dir, "config", "user.name", "Ouvrage")

        # Write README.md
        readme_path = os.path.join(tmp_dir, "README.md")
        with open(readme_path, "w") as f:
            f.write(f"# {project_id}\n\nInitialized by Ouvrage.\n")

        await _run_as_worker("git", "-C", tmp_dir, "add", "README.md")
        _, stderr, rc = await _run_as_worker(
            "git", "-C", tmp_dir, "commit", "-m", "Initial commit",
        )
        if rc != 0:
            raise RuntimeError(f"git commit (seed) failed: {stderr.decode()}")

        # Push to origin using auth URL so credentials work
        push_url = auth_url or "origin"
        _, stderr, rc = await _run_as_worker(
            "git", "-C", tmp_dir, "push", push_url, f"HEAD:{default_branch}",
        )
        if rc != 0:
            raise RuntimeError(f"git push (seed) failed: {stderr.decode()}")

        log.info(f"Seeded initial commit on '{default_branch}' for project '{project_id}'")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Re-fetch bare repo so origin/* refs are updated
    fetch_url = auth_url or "origin"
    await _run_as_worker(
        "git", "-C", bare_path, "fetch", fetch_url,
        "+refs/heads/*:refs/remotes/origin/*",
    )


async def setup_worktree(project: dict, dir_name: str, branch: str,
                         depends_on: str | None = None,
                         base_branch: str | None = None) -> str:
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
        log.debug(f"Worktree already exists: {worktree_path}, pulling latest")
        # Fetch + pull so resumed tasks pick up upstream changes
        _, fetch_err, fetch_rc = await _run_as_worker(
            "git", "-C", worktree_path, "fetch", "origin",
        )
        if fetch_rc != 0:
            log.warning(f"git fetch origin failed for {worktree_path}: {fetch_err.decode().strip()}, trying authenticated URL")
            try:
                from switchboard.git.operations import _resolve_push_url
                auth_url = await _resolve_push_url(project["id"])
                _, fallback_err, fallback_rc = await _run_as_worker(
                    "git", "-C", worktree_path, "fetch", auth_url,
                    "+refs/heads/*:refs/remotes/origin/*",
                )
                if fallback_rc != 0:
                    raise RuntimeError(
                        f"git fetch failed with origin and authenticated URL: {fallback_err.decode().strip()}"
                    )
                log.debug(f"Fallback fetch via authenticated URL succeeded for {worktree_path}")
            except ValueError as e:
                raise RuntimeError(f"git fetch failed and no PAT available for fallback: {e}")
        stdout, stderr, rc = await _run_as_worker(
            "git", "-C", worktree_path, "merge", "--ff-only",
            f"origin/{branch}",
        )
        if rc != 0:
            # Non-fatal — branch may not exist on remote yet, or diverged
            log.debug(f"Auto-pull skipped (ff-only failed): {stderr.decode().strip()}")
        return worktree_path

    # Ensure base directory exists (created as worker user so ownership is correct)
    await _run_as_worker("mkdir", "-p", base)

    # Clone the repo as a bare repo if the base doesn't have .git
    bare_path = os.path.join(base, ".bare")

    # Resolve authenticated URL once — used for both initial bare clone and fetch.
    # This ensures new private repos can clone without a credential helper pre-configured.
    # Must be resolved before the bare-path check so the clone gets credentials.
    try:
        from switchboard.git.operations import _resolve_push_url
        auth_url = await _resolve_push_url(project["id"])
    except Exception:
        auth_url = None  # no PAT configured or public repo — unauthenticated URL is fine

    if not os.path.exists(bare_path):
        log.info(f"Cloning bare repo: {project['repo']} -> {bare_path}")
        clone_url = auth_url or project["repo"]
        stdout, stderr, rc = await _run_as_worker(
            "git", "clone", "--bare", clone_url, bare_path,
        )
        if rc != 0:
            raise RuntimeError(f"git clone --bare failed: {stderr.decode()}")
        # bare clones lack a fetch refspec — set one so origin/* refs are created
        await _run_as_worker(
            "git", "-C", bare_path, "config",
            "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*",
        )
        # Strip PAT from bare repo config — git clone stores the URL used as
        # remote.origin.url, which would leave the PAT in plaintext on disk.
        # Reset to the plain repo URL; fetches already use auth_url directly.
        if auth_url:
            await _run_as_worker(
                "git", "-C", bare_path, "config",
                "remote.origin.url", project["repo"],
            )

    # Fetch latest from remote — use authenticated URL if available (avoids
    # dependency on credential.helper which may point to a deleted worktree script)
    fetch_url = auth_url or "origin"
    # When fetching by URL (not remote name), must pass refspec explicitly
    # or git only fetches HEAD without updating origin/* tracking refs
    fetch_args = ["git", "-C", bare_path, "fetch", fetch_url]
    if fetch_url != "origin":
        fetch_args.append("+refs/heads/*:refs/remotes/origin/*")
    _, fetch_err, fetch_rc = await _run_as_worker(*fetch_args)
    if fetch_rc != 0:
        log.warning(f"git fetch failed (rc={fetch_rc}): {fetch_err.decode().strip()}")

    # Seed an initial commit if the repo is empty (zero commits).
    # git worktree add requires at least one commit to exist as a base ref.
    await _seed_empty_repo(bare_path, project["id"], project["default_branch"], auth_url)

    # Auto-detect default branch from bare clone HEAD if project config is wrong
    default_branch = project["default_branch"]
    stdout, _, _ = await _run_as_worker("git", "-C", bare_path, "symbolic-ref", "HEAD")
    detected = stdout.decode().strip().removeprefix("refs/heads/")
    if detected and detected != default_branch:
        log.debug(f"Auto-detected default branch '{detected}' (project config said '{default_branch}')")
        default_branch = detected

    # Priority: depends_on (chain from parent) > base_branch (explicit) > origin/{default}
    base_ref = f"origin/{default_branch}"
    if depends_on:
        parent_task = await db.get_task(depends_on)
        if parent_task and parent_task.get("branch"):
            base_ref = f"origin/{parent_task['branch']}"
            log.debug(f"Branch chaining: branching from parent branch '{base_ref}' (depends_on={depends_on})")
    elif base_branch:
        base_ref = base_branch if base_branch.startswith("origin/") else f"origin/{base_branch}"
        log.debug(f"Explicit base_branch: branching from '{base_ref}'")

    # If the branch already exists on origin (e.g. reopened task), use it as base
    # so the new worktree starts with all previous commits instead of fresh from main.
    remote_ref = f"origin/{branch}"
    _, _, rev_rc = await _run_as_worker(
        "git", "-C", bare_path, "rev-parse", "--verify", remote_ref,
    )
    if rev_rc == 0 and not depends_on:
        log.debug(f"Branch '{branch}' exists on origin — using {remote_ref} as base (reopened task)")
        base_ref = remote_ref

    stdout, stderr, rc = await _run_as_worker(
        "git", "-C", bare_path, "worktree", "add",
        "-b", branch, worktree_path, base_ref,
    )
    if rc != 0:
        error_msg = stderr.decode()
        # Stale local branch ref — delete it and retry with -b (fresh branch from base)
        if "already exists" in error_msg:
            log.debug(f"Deleting stale branch ref '{branch}' and retrying")
            await _run_as_worker("git", "-C", bare_path, "branch", "-D", branch)
            stdout, stderr, rc = await _run_as_worker(
                "git", "-C", bare_path, "worktree", "add",
                "-b", branch, worktree_path, base_ref,
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

    # Make worktree group-writable so the service user (switchboard-svc) can
    # write files into worker-owned directories.
    await _run_as_worker("chmod", "g+w", worktree_path)

    # Lock git author to the worker's global config — CC workers sometimes
    # override user.name/email in the repo config, which sticks for all future tasks.
    await _run_as_worker("git", "-C", worktree_path, "config", "--unset", "user.name")
    await _run_as_worker("git", "-C", worktree_path, "config", "--unset", "user.email")

    # Ensure all remote refs are visible from the worktree.
    # Bare-repo worktrees have a narrow fetch refspec by default.
    await _run_as_worker(
        "git", "-C", worktree_path, "config",
        "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*",
    )

    return worktree_path


async def run_setup_command(project: dict, worktree_path: str, env_overrides: dict | None = None):
    """Run project setup command in the worktree."""
    cmd = project.get("setup_command")
    if not cmd:
        return

    log.debug(f"Running setup: {cmd} in {worktree_path}")
    stdout, stderr, rc = await _run_as_worker("sh", "-c", f"cd {shlex.quote(worktree_path)} && {cmd}")
    if rc != 0:
        log.warning(f"Setup command failed (exit {rc}): {stderr.decode()}")

    # Write env overrides AFTER setup (setup may create .env.testing from template)
    # Uses > (write mode) to recreate fresh each time, preventing duplicate entries on retry
    overrides = env_overrides
    if not overrides and project.get("env_overrides"):
        overrides = project["env_overrides"]
        if isinstance(overrides, str):
            overrides = json.loads(overrides)

    if overrides:
        env_path = os.path.join(worktree_path, ".env.testing")
        env_content = "\n".join(f"{k}={v}" for k, v in overrides.items()) + "\n"
        # Write fresh each time — prevents duplicate entries on retry/resume
        await _run_as_worker(
            "sh", "-c", f"cat > {shlex.quote(env_path)} << 'ENVEOF'\n{env_content}ENVEOF"
        )
        log.debug(f"Wrote env overrides to {env_path}")


async def setup_credential_helper(worktree_path: str, project_id: str) -> str | None:
    """Write a git credential helper script in /tmp for CC's direct git pushes.

    Resolves the GitHub PAT (project override → instance → skip if none),
    writes a bash script that outputs username/password, and configures the worktree's
    git credential.helper to use it. Also sets the remote to HTTPS so CC's git push works.

    Returns the path to the helper script, or None if no PAT is configured.
    The script lives in /tmp (not the worktree) and is deleted during worktree teardown.
    """
    # Lazy import to avoid circular dependency (operations.py imports worktree.py)
    from switchboard.git.operations import normalize_repo_url

    try:
        pat = await get_github_pat(project_id)
    except ValueError:
        log.debug(f"No GitHub PAT for project {project_id} — skipping credential helper setup")
        return None

    # Write credential helper script to /tmp — outside the worktree so CC workers
    # don't see it when exploring the directory. Path is unique per worktree via hash.
    path_hash = hashlib.sha256(worktree_path.encode()).hexdigest()[:12]
    helper_path = f"/tmp/ouvrage-creds-{path_hash}.sh"
    script_content = f"#!/bin/bash\necho 'username=oauth2'\necho 'password={pat}'\n"
    with open(helper_path, "w") as f:
        f.write(script_content)
    os.chmod(helper_path, 0o750)

    # Configure git in the worktree to use the helper — worktree-scoped only
    # so it doesn't leak into the bare repo config and poison future fetches.
    # Must also set core.bare=false in worktree config because the bare repo
    # has core.bare=true and worktreeConfig extension would inherit it.
    bare_path = os.path.join(os.path.dirname(worktree_path), ".bare")
    await _run_as_worker(
        "git", "-C", bare_path, "config", "extensions.worktreeConfig", "true",
    )
    await _run_as_worker(
        "git", "-C", worktree_path, "config", "--worktree", "core.bare", "false",
    )
    await _run_as_worker(
        "git", "-C", worktree_path, "config", "--worktree", "credential.helper", helper_path,
    )

    # Ensure remote is HTTPS (not SSH) — worktree-scoped
    project = await db.get_project(project_id)
    if project and project.get("repo"):
        https_url = normalize_repo_url(project["repo"])
        await _run_as_worker(
            "git", "-C", worktree_path, "config", "--worktree", "remote.origin.url", https_url,
        )

    log.debug(f"Configured credential helper for worktree {worktree_path}")
    return helper_path


async def cleanup_worktree(project: dict, task: dict, force_delete_branch: bool = False):
    """Remove worktree and optionally delete branch."""
    worktree_path = task.get("worktree_path")
    bare_path = os.path.join(project["working_dir"], ".bare")

    # Run teardown command
    teardown = project.get("teardown_command")
    if teardown and worktree_path and os.path.exists(worktree_path):
        log.debug(f"Running teardown: {teardown}")
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

    # Clean up credential helper from /tmp
    if worktree_path:
        path_hash = hashlib.sha256(worktree_path.encode()).hexdigest()[:12]
        cred_path = f"/tmp/ouvrage-creds-{path_hash}.sh"
        try:
            os.unlink(cred_path)
            log.debug(f"Removed credential helper: {cred_path}")
        except FileNotFoundError:
            pass

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
            log.debug(f"Deleted branch: {branch}")
        else:
            log.debug(f"Branch delete skipped (not merged or not found): {branch}")
