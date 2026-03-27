"""Git branch operations — rebase, merge, push, PR creation, diff extraction."""

import asyncio
import logging
import os
import re

import httpx

import switchboard.db as db
from switchboard.db.users import get_github_pat
from switchboard.git.worktree import _run_as_worker

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repo URL parsing and authenticated URL construction
# ---------------------------------------------------------------------------

_SSH_PATTERN = re.compile(r"^git@github\.com:([^/]+)/(.+?)(?:\.git)?$")
_HTTPS_PATTERN = re.compile(r"^https?://github\.com/([^/]+)/(.+?)(?:\.git)?$")


def parse_repo_url(url: str) -> tuple[str, str]:
    """Parse a GitHub repo URL into (owner, repo).

    Handles:
        git@github.com:owner/repo.git
        git@github.com:owner/repo
        https://github.com/owner/repo.git
        https://github.com/owner/repo
    """
    m = _SSH_PATTERN.match(url)
    if m:
        return m.group(1), m.group(2)
    m = _HTTPS_PATTERN.match(url)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"Cannot parse GitHub owner/repo from URL: {url}")


def normalize_repo_url(url: str) -> str:
    """Normalize any GitHub repo URL to canonical HTTPS format.

    git@github.com:owner/repo.git  → https://github.com/owner/repo.git
    git@github.com:owner/repo      → https://github.com/owner/repo.git
    https://github.com/owner/repo  → https://github.com/owner/repo.git
    """
    owner, repo = parse_repo_url(url)
    return f"https://github.com/{owner}/{repo}.git"


def _build_authenticated_url(pat: str, repo_url: str) -> str:
    """Build an HTTPS push URL with PAT embedded. Never store this — use at call time only."""
    owner, repo = parse_repo_url(repo_url)
    return f"https://oauth2:{pat}@github.com/{owner}/{repo}.git"


async def _resolve_push_url(project_id: str) -> str:
    """Resolve PAT and project repo URL into an authenticated HTTPS push URL."""
    pat = await get_github_pat(project_id)
    project = await db.get_project(project_id)
    if not project:
        raise ValueError(f"Project {project_id} not found")
    return _build_authenticated_url(pat, project["repo"])


def _classify_push_error(stderr_text: str) -> str | None:
    """Classify git push stderr into a user-friendly error message, or None if unrecognized."""
    s = stderr_text.lower()
    if "authentication failed" in s or "invalid credentials" in s or "401" in s:
        return "GitHub PAT is invalid or expired. Update it in settings."
    if "403" in s or ("permission" in s and "denied" in s):
        return "GitHub PAT lacks push permission. Ensure it has `repo` scope."
    if "not found" in s or "404" in s:
        return "Repository not found. Check that the PAT has access to this repo."
    if "could not resolve host" in s or "unable to access" in s:
        return "Could not reach GitHub. Will retry."
    return None


# ---------------------------------------------------------------------------
# GitHub REST API — PR creation
# ---------------------------------------------------------------------------

async def create_github_pr(
    pat: str, owner: str, repo: str, head: str, base: str,
    title: str, body: str = "",
) -> dict:
    """Create a GitHub PR via REST API. Returns {url, number}.

    Handles 422 "already exists" by finding the existing PR.
    """
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        resp = await client.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )

        if resp.status_code == 201:
            data = resp.json()
            return {"url": data["html_url"], "number": data["number"]}

        if resp.status_code == 422:
            errors = resp.json()
            if "already exists" in str(errors).lower():
                return await _find_existing_pr(client, owner, repo, head)
            raise ValueError(f"PR creation failed: {errors}")

        if resp.status_code == 404:
            raise ValueError(f"Repository not found: {owner}/{repo}")
        if resp.status_code == 403:
            raise ValueError(f"PAT lacks permission to create PRs. Ensure it has `repo` scope.")

        resp.raise_for_status()
        return {}  # unreachable, but satisfies type checker


async def _find_existing_pr(
    client: httpx.AsyncClient, owner: str, repo: str, head: str,
) -> dict:
    """Find an existing open PR for the given head branch."""
    resp = await client.get(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        params={"head": f"{owner}:{head}", "state": "open"},
    )

    if resp.status_code == 200:
        prs = resp.json()
        if prs:
            return {"url": prs[0]["html_url"], "number": prs[0]["number"]}
    raise ValueError(f"PR already exists for {head} but could not find it")


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
    """Force-push task branch if there are unpushed commits.

    Uses HTTPS + PAT for authentication instead of SSH.
    """
    worktree = task.get("worktree_path")
    branch = task.get("branch")
    if not worktree or not branch or not os.path.exists(worktree):
        return

    # Resolve authenticated push URL
    try:
        push_url = await _resolve_push_url(task["project_id"])
    except ValueError as e:
        log.warning(f"Cannot push {task_id}: {e}")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Push failed — no PAT configured",
            content=str(e),
        )
        return

    # Check if remote branch exists
    stdout, _, rc = await _run_as_worker(
        "git", "-C", worktree, "ls-remote", "--heads", push_url, branch,
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
        "git", "-C", worktree, "push", "--force-with-lease", push_url, branch,
    )
    if rc != 0:
        stderr_text = stderr.decode()
        friendly = _classify_push_error(stderr_text)
        log.warning(f"Auto-push failed for {task_id}: {stderr_text}")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Auto-push failed",
            content=friendly or f"```\n{stderr_text[:1000]}\n```",
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
    """Create PR if auto_pr is enabled and this is the tail of a chain.

    Uses GitHub REST API with PAT instead of gh CLI.
    """
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

    # Resolve PAT and parse owner/repo
    try:
        pat = await get_github_pat(task["project_id"])
        owner, repo = parse_repo_url(project["repo"])
    except ValueError as e:
        log.warning(f"PR creation skipped for {task_id}: {e}")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="PR creation failed — no PAT configured",
            content=str(e),
        )
        return

    # Walk the chain to collect goals
    chain = await db.get_chain(task_id)
    goals = [t["goal"] for t in chain if not t.get("parent_task_id")]  # Exclude review tasks

    title = task["goal"][:70]
    body = "## Summary\n" + "\n".join(f"- {g}" for g in goals)

    log.info(f"Auto-creating PR for {task_id}: {branch} → {default_branch}")
    try:
        result = await create_github_pr(
            pat=pat, owner=owner, repo=repo,
            head=branch, base=default_branch,
            title=title, body=body,
        )
        pr_url = result["url"]
        await db.add_artifact(task_id, "pr_url", pr_url)
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="PR Created",
            content=f"[{pr_url}]({pr_url})",
        )
        log.info(f"PR created for {task_id}: {pr_url}")
    except Exception as e:
        log.warning(f"PR creation failed for {task_id}: {e}")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="PR creation failed",
            content=str(e),
        )


async def _perform_auto_merge(task_id: str) -> bool:
    """Merge task branch into branch_target using detached HEAD. Returns True on success.

    Uses `git checkout --detach origin/<target>` instead of checking out the target
    branch by name, which avoids the "fatal: a branch named 'X' already exists"
    error that occurs when the target branch is checked out in another worktree.

    Uses HTTPS + PAT for fetch and push operations.
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

    # Resolve authenticated push URL
    try:
        push_url = await _resolve_push_url(task["project_id"])
    except ValueError as e:
        log.error(f"Auto-merge {task_id}: {e}")
        await db.update_task(task_id, pr_status="error", pr_error=str(e))
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Auto-merge failed — no PAT configured",
            content=str(e),
        )
        return False

    try:
        for attempt in range(1, 4):
            # Fetch latest target branch via authenticated URL
            await _run_as_worker("git", "-C", worktree, "fetch", push_url, branch_target)

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

            # Push HEAD to target branch via authenticated URL
            _, stderr, rc = await _run_as_worker(
                "git", "-C", worktree, "push", push_url, f"HEAD:{branch_target}",
            )
            if rc == 0:
                break  # push succeeded

            # Push rejected — likely a race with another task; retry
            stderr_text = stderr.decode()
            log.warning(f"Auto-merge {task_id}: push attempt {attempt} rejected, retrying...")
            if attempt == 3:
                friendly = _classify_push_error(stderr_text)
                log.error(f"Auto-merge {task_id}: push failed after 3 attempts: {stderr_text}")
                await db.update_task(task_id, status="needs-review",
                                     pr_status="push-failed", pr_error=stderr_text[:500])
                await db.post_task_message(
                    task_id=task_id, author="dispatcher", type="status",
                    title="Auto-merge push failed",
                    content=friendly or f"Merge succeeded but push to `{branch_target}` failed after 3 attempts:\n```\n{stderr_text[:1000]}\n```",
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
