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
import logging

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
# 1. setup_task_worktree
# ---------------------------------------------------------------------------

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

    # Configure credential helper so CC's direct git pushes use PAT auth
    await _engine.setup_credential_helper(worktree_path, task["project_id"])

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

async def launch_sdk_session(
    task_id: str,
    prompt: str,
    worktree_path: str,
    session_id: str | None = None,
    is_resume: bool = False,
    max_turns: int = 200,
    max_wall_clock: int = 60,
    model: str = "sonnet",
) -> asyncio.Task:
    """Setup log dir, write dispatch log, launch _run_sdk_session in background.

    Returns the asyncio.Task handle. Does NOT check or set task status.
    Adds the task to _running_tasks with exception handler.
    """
    import switchboard.dispatch.engine as _engine

    log_dir = await _engine._setup_log_dir(worktree_path, clean=not is_resume)

    _engine._write_dispatch_log(
        log_dir, task_id, session_id or "(new)",
        max_turns, max_wall_clock,
        worktree_path, is_resume, model,
    )

    task_handle = asyncio.create_task(
        _engine._run_sdk_session(
            task_id=task_id,
            prompt=prompt,
            worktree_path=worktree_path,
            session_id=session_id,
            is_resume=is_resume,
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
            if m.get("author") != "dispatcher"
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
