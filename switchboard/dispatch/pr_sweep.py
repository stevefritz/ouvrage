"""switchboard.dispatch.pr_sweep — background GitHub PR status polling.

Polls GitHub every 60s for all tasks that have an open PR (pr_url set but
pr_status is not 'merged' or 'closed'). Updates task.pr_status when it changes,
and handles the merge transition (post message, advance status to 'merged').
"""

import asyncio
import logging
import re

import httpx

import switchboard.db as db
from switchboard.db.users import get_github_pat

log = logging.getLogger(__name__)

# Matches https://github.com/{owner}/{repo}/pull/{number}
_PR_URL_RE = re.compile(
    r"https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)"
)


def _parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Parse a GitHub PR URL into (owner, repo, pr_number).

    Raises ValueError if the URL doesn't match the expected pattern.
    """
    m = _PR_URL_RE.match(pr_url.strip())
    if not m:
        raise ValueError(f"Cannot parse PR URL: {pr_url!r}")
    return m.group("owner"), m.group("repo"), int(m.group("number"))


async def _check_pr_status(pr_url: str, project_id: str) -> str:
    """Call the GitHub API and return 'open', 'merged', or 'closed'.

    Raises on HTTP errors or invalid URLs.
    """
    owner, repo, pr_number = _parse_pr_url(pr_url)
    pat = await get_github_pat(project_id)

    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(api_url, headers=headers)
        response.raise_for_status()
        data = response.json()

    merged = data.get("merged", False)
    state = data.get("state", "open")

    if merged:
        return "merged"
    if state == "closed":
        return "closed"
    return "open"


async def _handle_pr_merged(task: dict) -> None:
    """Handle a PR that just merged.

    1. Post a status message to the task thread.
    2. If task status is 'completed' and gate_status is 'passed', transition to 'merged'.
    """
    task_id = task["id"]
    pr_url = task.get("pr_url", "")

    # Extract PR number for the message
    pr_number = None
    try:
        _, _, pr_number = _parse_pr_url(pr_url)
    except ValueError:
        pass

    number_str = f"#{pr_number}" if pr_number else ""
    await db.post_task_message(
        task_id=task_id,
        author="dispatcher",
        type="status",
        title="PR merged",
        content=f"✅ PR {number_str} merged on GitHub",
    )

    # Transition to 'merged' if the task is completed and gate passed
    if task.get("status") == "completed" and task.get("gate_status") == "passed":
        await db.update_task(task_id, status="merged")
        log.info(f"PR sweep: task {task_id} transitioned to 'merged' after PR merge")


async def _pr_status_sweep() -> None:
    """Background loop: poll GitHub every 60s for PR status changes."""
    while True:
        await asyncio.sleep(60)
        try:
            tasks = await db.get_tasks_with_open_prs()
        except Exception as e:
            log.warning(f"PR sweep: failed to fetch tasks: {e}")
            continue

        for task in tasks:
            try:
                pr_url = task.get("pr_url")
                if not pr_url:
                    continue
                project_id = task.get("project_id")
                status = await _check_pr_status(pr_url, project_id)
                if status != task.get("pr_status"):
                    await db.update_task(task["id"], pr_status=status)
                    log.info(
                        f"PR sweep: task {task['id']} pr_status {task.get('pr_status')!r} → {status!r}"
                    )
                    if status == "merged":
                        await _handle_pr_merged(task)
            except Exception as e:
                log.warning(f"PR status check failed for {task['id']}: {e}")
