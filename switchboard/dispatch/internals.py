"""switchboard.dispatch.internals — status-agnostic dispatch building blocks.

Extracted from dispatch_task (engine.py) so that lifecycle side effects can
reuse worktree setup, config resolution, prompt building, and session launch
without triggering dispatch_task's status validation.

All functions in this module are **status-agnostic**: they do NOT check or
set task status, and they do NOT call db.update_task(status=...).
They do NOT import from lifecycle.py (no circular dependency).

Functions that call git/session operations use function-level imports from
engine.py so that existing test patches on engine.* bindings still apply.
"""

import asyncio
import json
import logging
import os
import shlex
from pathlib import Path

import switchboard.db as db
from switchboard.config.constants import DEFAULT_MODEL
from switchboard.dispatch._state import _running_tasks

log = logging.getLogger(__name__)


def _resolve_limit(task_val, project_val, global_default):
    """Resolve a limit: task override > project default > global default."""
    if task_val is not None:
        return task_val
    if project_val is not None:
        return project_val
    return global_default


# ---------------------------------------------------------------------------
# 1. setup_task_worktree / checkout_existing_worktree
# ---------------------------------------------------------------------------

async def checkout_existing_worktree(project: dict, task: dict) -> str:
    """Checkout an existing branch from origin into a worktree. Returns worktree_path.

    For reopen/resume — checks out the branch as it exists on origin.
    No depends_on logic, no branch deletion, no rebasing.
    Falls back to setup_task_worktree if the branch doesn't exist on origin.
    """
    import switchboard.dispatch.engine as _engine
    from switchboard.git.worktree import _run_as_worker

    task_id = task["id"]
    short_name = task_id.split("/")[-1] if "/" in task_id else task_id
    branch = task["branch"] or short_name
    base = project["working_dir"]
    worktree_path = os.path.join(base, short_name)
    bare_path = os.path.join(base, ".bare")

    # If worktree already exists, just fetch and pull
    if os.path.exists(worktree_path):
        log.debug(f"Worktree already exists: {worktree_path}, fetching latest")
        await _run_as_worker("git", "-C", worktree_path, "fetch", "origin")
        await _run_as_worker(
            "git", "-C", worktree_path, "merge", "--ff-only",
            f"origin/{branch}",
        )
        await setup_hook_config(worktree_path)
        return worktree_path

    # Fetch so origin refs are current
    await _run_as_worker("git", "-C", bare_path, "fetch", "origin")

    # Check if branch exists on origin
    _, _, rc = await _run_as_worker(
        "git", "-C", bare_path, "rev-parse", "--verify", f"origin/{branch}",
    )

    if rc != 0:
        # Branch doesn't exist on origin — fall back to full setup
        log.debug(f"Branch '{branch}' not on origin, falling back to setup_task_worktree")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="New worktree",
            content=f"Branch `{branch}` not found on origin — creating fresh worktree.",
        )
        return await setup_task_worktree(project, task)

    # Delete stale local ref if it exists (prevents worktree add conflict)
    await _run_as_worker("git", "-C", bare_path, "branch", "-D", branch)

    # Checkout from origin — no -b flag reuse, create branch tracking origin
    stdout, stderr, rc = await _run_as_worker(
        "git", "-C", bare_path, "worktree", "add",
        "-b", branch, worktree_path, f"origin/{branch}",
    )
    if rc != 0:
        raise RuntimeError(f"Failed to checkout existing branch: {stderr.decode()}")

    # Make worktree group-writable for service user
    await _run_as_worker("chmod", "g+w", worktree_path)

    log.debug(f"Checked out existing branch '{branch}' from origin into {worktree_path}")
    await db.post_task_message(
        task_id=task_id, author="dispatcher", type="status",
        title="Worktree restored",
        content=f"Branch `{branch}` found on origin — checked out existing work.",
    )

    await setup_hook_config(worktree_path)
    await _engine.run_setup_command(project, worktree_path)

    return worktree_path


async def setup_hook_config(worktree_path: str) -> None:
    """Write PreToolUse hooks into {worktree}/.claude/settings.json.

    Overwrites any existing file unconditionally — never merges repo content.
    A malicious repo could include PreToolUse hooks that exfiltrate secrets;
    overwriting ensures only Ouvrage-controlled hooks are active.

    The hook scripts live at /opt/switchboard/hooks/ on the host —
    outside the worktree so CC cannot edit them.
    """
    settings_dir = os.path.join(worktree_path, ".claude")
    settings_path = os.path.join(settings_dir, "settings.json")

    # Build Ouvrage's hooks from scratch — do NOT read or preserve any repo content.
    # Any repo-defined hooks (e.g. malicious PreToolUse exfil scripts) are discarded.
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "if": "Bash(git push*)",
                            "command": "/opt/switchboard/hooks/block-git-push.sh",
                        },
                        {
                            "type": "command",
                            "if": "Bash(git fetch*)",
                            "command": "/opt/switchboard/hooks/block-git-fetch.sh",
                        },
                    ],
                }
            ]
        }
    }

    # Write as worker user — the worktree is owned by the worker, and .claude/
    # from the repo checkout is not group-writable.
    from switchboard.git.worktree import _run_as_worker
    await _run_as_worker("mkdir", "-p", settings_dir)
    settings_json = json.dumps(settings, indent=2)
    await _run_as_worker(
        "sh", "-c", f"cat > {shlex.quote(settings_path)} << 'HOOKEOF'\n{settings_json}\nHOOKEOF"
    )

    log.debug(f"Wrote hook config to {settings_path}")


async def setup_task_worktree(project: dict, task: dict) -> str:
    """Create worktree, setup credentials, run setup_command. Returns worktree_path.

    Idempotent — if worktree already exists and is valid, reuses it.
    Does NOT check or set task status.
    """
    # Lazy import: read from engine's namespace so test patches on
    # engine.setup_worktree / engine.run_setup_command still apply.
    import switchboard.dispatch.engine as _engine

    task_id = task["id"]
    short_name = task_id.split("/")[-1] if "/" in task_id else task_id
    effective_branch = task["branch"] or short_name

    if task["branch"] != effective_branch:
        await db.update_task(task_id, branch=effective_branch)

    worktree_path = await _engine.setup_worktree(
        project, short_name, effective_branch,
        depends_on=task.get("depends_on"),
        base_branch=task.get("base_branch"),
    )

    # Write hook config to block direct git push/fetch
    await setup_hook_config(worktree_path)

    # Run setup command
    await _engine.run_setup_command(project, worktree_path)

    return worktree_path


# ---------------------------------------------------------------------------
# 2. resolve_session_config
# ---------------------------------------------------------------------------

def resolve_session_config(task: dict, project: dict) -> dict:
    """Resolve max_turns, max_wall_clock, model from task -> project -> global defaults.

    Returns {"max_turns": int, "max_wall_clock": int, "model": str}.
    Does NOT check or set task status.
    """
    return {
        "max_turns": _resolve_limit(
            task.get("max_turns"), project.get("max_turns"), db.DEFAULT_MAX_TURNS,
        ),
        "max_wall_clock": _resolve_limit(
            task.get("max_wall_clock"), project.get("max_wall_clock"), db.DEFAULT_MAX_WALL_CLOCK,
        ),
        "model": _resolve_limit(
            task.get("model"), project.get("model"), DEFAULT_MODEL,
        ),
    }


# ---------------------------------------------------------------------------
# 3. build_dispatch_prompt
# ---------------------------------------------------------------------------

async def build_dispatch_prompt(
    project: dict, task: dict,
    escalation_criteria: str | None = None,
    review_feedback: list[dict] | None = None,
) -> str:
    """Build the CC prompt for dispatch/retry/start.

    Reads pinned spec and checklist from DB. Calls _build_task_prompt.
    Does NOT check or set task status.
    """
    import switchboard.dispatch.engine as _engine

    task_id = task["id"]

    spec_content = None
    pinned = await db.get_task_pinned(task_id)
    if pinned:
        spec_content = pinned["content"]

    checklist_items = await db.get_checklist(task_id)

    prompt = await _engine._build_task_prompt(
        project, task, spec_content, checklist_items,
        escalation_criteria, review_feedback,
    )
    return prompt


# ---------------------------------------------------------------------------
# 4. launch_sdk_session
# ---------------------------------------------------------------------------

async def _copy_archived_session_log(
    task_id: str, log_dir: Path, fork_session_id: str,
) -> None:
    """Copy the previous attempt's session.jsonl from archive into the new log dir.

    Reads from .task-history/{slug}/attempt-{prev}/, appends a fork marker,
    and writes into the new attempt's session.jsonl. The archive is untouched.
    """
    import switchboard.dispatch.engine as _engine
    from switchboard.dispatch.sdk_session import _open_shared

    task = await db.get_task(task_id)
    if not task:
        return
    project = await db.get_project(task["project_id"])
    if not project:
        return

    current_attempt = task.get("current_attempt") or 1
    prev_attempt = current_attempt - 1
    if prev_attempt < 1:
        return

    archive_dir = _engine._find_archive_path(project, task_id, prev_attempt)
    if not archive_dir:
        return

    archived_log = archive_dir / "session.jsonl"
    if not archived_log.exists():
        return

    try:
        prev_content = archived_log.read_text()
        if not prev_content.strip():
            return

        session_log = log_dir / "session.jsonl"
        with _open_shared(session_log) as f:
            f.write(prev_content)
            if not prev_content.endswith("\n"):
                f.write("\n")
            marker = {
                "timestamp": db.now_iso(),
                "type": "SystemMessage",
                "subtype": "fork",
                "forked_from_session": fork_session_id,
                "forked_from_attempt": prev_attempt,
            }
            f.write(json.dumps(marker) + "\n")
        log.debug(f"Copied session log from attempt {prev_attempt} into attempt {current_attempt} for {task_id}")
    except Exception as e:
        log.warning(f"Failed to copy archived session log for {task_id}: {e}")


async def launch_sdk_session(
    task_id: str,
    prompt: str,
    worktree_path: str,
    session_id: str | None = None,
    is_resume: bool = False,
    fork_session_id: str | None = None,
    max_turns: int = 200,
    max_wall_clock: int = 60,
    model: str = "sonnet",
) -> asyncio.Task:
    """Setup log dir, write dispatch log, launch _run_sdk_session in background.

    Returns the asyncio.Task handle. Does NOT check or set task status.
    Adds the task to _running_tasks with exception handler.

    fork_session_id: if set, the new session forks from this session_id
    (inherits full message history but diverges). Used for retries.
    """
    import switchboard.dispatch.engine as _engine

    log_dir = await _engine._setup_log_dir(worktree_path, clean=not is_resume)

    # On fork: copy previous attempt's session log from archive into new log dir
    if fork_session_id:
        await _copy_archived_session_log(task_id, log_dir, fork_session_id)

    _engine._write_dispatch_log(
        log_dir, task_id, session_id or fork_session_id or "(new)",
        max_turns, max_wall_clock,
        worktree_path, is_resume, model,
        forked=bool(fork_session_id),
        fork_parent_session=fork_session_id,
    )

    task_handle = asyncio.create_task(
        _engine._run_sdk_session(
            task_id=task_id,
            prompt=prompt,
            worktree_path=worktree_path,
            session_id=session_id,
            is_resume=is_resume,
            fork_session_id=fork_session_id,
            max_turns=max_turns,
            max_wall_clock_minutes=max_wall_clock,
            log_dir=log_dir,
            model=model,
        ),
        name=f"sdk-session-{task_id}",
    )
    _running_tasks.add(task_handle)
    task_handle.add_done_callback(_engine._handle_task_exception)

    return task_handle


# ---------------------------------------------------------------------------
# 5. check_and_queue_if_full
# ---------------------------------------------------------------------------

async def check_and_queue_if_full(task_id: str) -> bool:
    """Check concurrency limit. If full, queue the task and return True.

    Returns True if queued (caller should return early), False if slot available.
    Does NOT check or set task status.
    """
    active = await db.count_active_tasks()
    limit = await db.get_concurrency_limit()
    if active >= limit:
        queued_at = db.now_iso()
        await db.update_task(task_id, queued_at=queued_at)
        log.info(f"Task {task_id} queued (concurrency full: {active}/{limit})")
        return True
    return False


# ---------------------------------------------------------------------------
# 6. collect_review_feedback
# ---------------------------------------------------------------------------

async def collect_review_feedback(task_id: str) -> list[dict] | None:
    """Find review/feedback messages posted after the last CC result.

    Returns list of feedback message dicts or None.
    Does NOT check or set task status.
    """
    thread = await db.read_task_messages(task_id)
    messages = thread.get("messages", [])

    last_result_idx = None
    for i, msg in enumerate(messages):
        if msg.get("author") == "cc-worker" and msg.get("type") == "result":
            last_result_idx = i

    if last_result_idx is not None:
        feedback = [
            m for m in messages[last_result_idx + 1:]
            if m.get("author") != "dispatcher" or m.get("type") in ("test-result", "review")
        ]
        if feedback:
            return feedback

    return None


# ---------------------------------------------------------------------------
# 7. collect_reopen_feedback
# ---------------------------------------------------------------------------

async def collect_reopen_feedback(task_id: str, current_attempt: int) -> list[dict] | None:
    """Find user feedback messages posted after the reopen status message.

    Returns list of feedback message dicts or None.
    Does NOT check or set task status.
    """
    thread = await db.read_task_messages(task_id)
    messages = thread.get("messages", [])

    # Find the index of the first message with the new attempt_number (the reopen status msg)
    reopen_msg_idx = None
    for i, msg in enumerate(messages):
        if (msg.get("attempt_number") or 1) == current_attempt:
            reopen_msg_idx = i
            break

    if reopen_msg_idx is not None:
        feedback = [
            m for m in messages[reopen_msg_idx + 1:]
            if m.get("author") not in ("switchboard", "dispatcher", "cc-worker")
        ]
        if feedback:
            return feedback

    return None
