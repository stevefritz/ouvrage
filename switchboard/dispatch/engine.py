"""switchboard.dispatch.engine — task lifecycle orchestration hub.

All public task operations: dispatch, resume, retry, reopen, cancel, close,
approve, skip_gate, advance_chain, cancel_chain, and component/project
pause/stop/resume controls.

Also owns:
  _check_and_dispatch_dependents  — post-gate chain progression
  _invalidate_chain               — downstream stale-marking
  _update_usage                   — SDK token/cost accumulation
  archive_task_logs / release_worktree / list_attempts — log and worktree ops

Shared mutable state (_running_tasks, _active_clients) lives in _state.py
to avoid circular imports. Sibling modules (gates.py, recovery.py, queue.py,
sdk_session.py) use lazy function-level imports from engine or _state when
they need to call back into engine functions.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from claude_agent_sdk import ClaudeSDKClient

import switchboard.db as db
from switchboard.notifications import slack as notify
from switchboard.config.constants import DEFAULT_MODEL
from switchboard.git.worktree import (
    _run_as_worker,
    setup_worktree,
    setup_credential_helper,
    cleanup_worktree,
    run_setup_command,
)
from switchboard.git.operations import (
    _git_fetch_and_rebase,
    _sync_branch_with_base,
    _ensure_branch_pushed,
    _maybe_create_pr,
    _perform_auto_merge,
)
from switchboard.dispatch._state import _running_tasks, _active_clients
from switchboard.dispatch.sdk_session import (
    _build_task_prompt,
    _setup_log_dir,
    _write_dispatch_log,
    _run_sdk_session,
)
from switchboard.dispatch.queue import _drain_queue

log = logging.getLogger(__name__)


def _handle_task_exception(task: asyncio.Task) -> None:
    """Log unhandled exceptions from background tasks and clean up tracking."""
    _running_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.error(f"Background task {task.get_name()} failed: {exc}", exc_info=exc)


def _resolve_limit(task_val, project_val, global_default):
    """Resolve a limit: task override > project default > global default."""
    if task_val is not None:
        return task_val
    if project_val is not None:
        return project_val
    return global_default


# ---------------------------------------------------------------------------
# Chain Logic
# ---------------------------------------------------------------------------

async def _check_and_dispatch_dependents(task_id: str) -> None:
    """Gate-pass post-processing: auto-merge, auto-release, chain advancement, queue drain."""
    task = await db.get_task(task_id)
    if not task or not task.get("gate_passed_at"):
        return

    # Resolve punchlist items claimed by this task
    resolved = await db.resolve_punchlist_items_for_task(task_id)
    if resolved:
        log.info(f"Task {task_id}: resolved {resolved} punchlist item(s)")

    # Auto-merge if enabled (before chain advancement)
    if task.get("auto_merge"):
        merge_ok = await _perform_auto_merge(task_id)
        if not merge_ok:
            return  # Conflict or error — don't advance chain

    dependents = await db.get_dependents(task_id)
    dispatched_any = False
    for dep in dependents:
        if dep["status"] == "ready" and not dep.get("held"):
            log.info(f"Auto-dispatching dependent task {dep['id']} (parent {task_id} gate passed)")
            try:
                await dispatch_task(
                    project_id=dep["project_id"],
                    task_id=dep["id"],
                    goal=dep["goal"],
                    auto_test=dep.get("auto_test", True),
                )
                dispatched_any = True
            except Exception as e:
                log.error(f"Failed to auto-dispatch dependent {dep['id']}: {e}")
        elif dep["status"] == "ready" and dep.get("held"):
            log.info(f"Skipping held task {dep['id']} — requires manual approval")
            await db.post_task_message(
                task_id=dep["id"], author="dispatcher", type="status",
                title="Ready but held",
                content="Parent task completed and gate passed. This task is held — approve to dispatch.",
            )
            continue
        elif dep.get("gate_status") == "stale" and dep["status"] in ("completed", "cancelled"):
            # Re-dispatch stale downstream task with rebase
            log.info(f"Re-dispatching stale dependent {dep['id']} (parent {task_id} gate passed)")
            try:
                await _rebase_and_redispatch(dep, task)
                dispatched_any = True
            except Exception as e:
                log.error(f"Failed to re-dispatch stale dependent {dep['id']}: {e}")

    # If no dependents to dispatch, this might be the chain tail — try auto-PR
    if not dispatched_any:
        await _maybe_create_pr(task_id)

    # Auto-release worktree AFTER PR creation so worktree_path is still available
    await _auto_release_worktree(task_id)

    # Drain FIFO queue — a slot may have opened up
    await _drain_queue()


async def _invalidate_chain(task_id: str) -> None:
    """Mark all downstream tasks as stale when a parent is re-dispatched."""
    dependents = await db.get_dependents(task_id)
    for dep in dependents:
        if dep["status"] == "working":
            try:
                await cancel_task(dep["id"])
            except Exception as e:
                log.error(f"Failed to cancel working dependent {dep['id']}: {e}")

        current_gate = dep.get("gate_status")
        if dep["status"] in ("completed", "ready") or current_gate in ("passed", "testing", "reviewing"):
            await db.update_task(
                dep["id"],
                gate_status="stale",
                gate_passed_at=None,
            )
            log.info(f"Marked {dep['id']} as stale (parent {task_id} re-dispatched)")

        # Recurse down the chain
        await _invalidate_chain(dep["id"])


async def _rebase_and_redispatch(dep: dict, parent: dict) -> None:
    """Rebase a stale task's branch onto parent's updated branch, then re-dispatch."""
    worktree = dep.get("worktree_path")
    dep_branch = dep.get("branch")
    parent_branch = parent.get("branch")

    if not worktree or not dep_branch or not parent_branch:
        log.warning(f"Cannot rebase {dep['id']}: missing worktree or branch info")
        return

    success = await _git_fetch_and_rebase(worktree, parent_branch)

    if not success:
        log.warning(f"Rebase failed for {dep['id']}, CC will handle manually")
        rebase_context = (
            f"WARNING: Automatic rebase onto the updated parent branch `{parent_branch}` failed "
            f"due to conflicts. Run `git rebase origin/{parent_branch}` and resolve conflicts, "
            "or cherry-pick your changes onto the updated parent."
        )
    else:
        rebase_context = (
            f"Your branch has been automatically rebased onto the updated parent branch "
            f"`{parent_branch}`. Review the parent's changes and evaluate if your work "
            "needs adjustment. If no rework is needed, just commit and finish."
        )

    # Reset gate state — fresh run
    await db.update_task(
        dep["id"],
        gate_status=None,
        gate_retries=0,
        gate_passed_at=None,
        session_id=None,
    )

    # Post context message
    await db.post_task_message(
        task_id=dep["id"], author="dispatcher", type="status",
        title="Re-dispatched (parent changed)",
        content=rebase_context,
    )

    # Re-dispatch with rebase context as review feedback
    await dispatch_task(
        project_id=dep["project_id"],
        task_id=dep["id"],
        goal=dep["goal"],
        phase="revisions",
        review_feedback=[{
            "author": "dispatcher",
            "title": "Parent Updated",
            "content": rebase_context,
        }],
    )


async def _update_usage(task_id: str, result) -> None:
    """Update task token/cost tracking from SDK result."""
    task = await db.get_task(task_id)
    input_tokens = 0
    output_tokens = 0

    if result.usage:
        # Claude Max usage format includes cache token breakdowns
        input_tokens = (
            result.usage.get("input_tokens", 0)
            + result.usage.get("cache_creation_input_tokens", 0)
            + result.usage.get("cache_read_input_tokens", 0)
        )
        output_tokens = result.usage.get("output_tokens", 0)

    cost = result.total_cost_usd or 0.0

    await db.update_task(
        task_id,
        total_input_tokens=(task.get("total_input_tokens") or 0) + input_tokens,
        total_output_tokens=(task.get("total_output_tokens") or 0) + output_tokens,
        total_cost_usd=(task.get("total_cost_usd") or 0.0) + cost,
    )


# ---------------------------------------------------------------------------
# Log Archive
# ---------------------------------------------------------------------------

def _task_slug(task_id: str) -> str:
    """Return filesystem-safe slug from task_id (last path component)."""
    return task_id.split("/")[-1] if "/" in task_id else task_id


async def archive_task_logs(task: dict, project: dict, reason: str) -> Path | None:
    """Copy .switchboard/ contents to persistent .task-history archive.

    Dest: {project.working_dir}/.task-history/{task_slug}/attempt-{dispatch_count}/
    Writes metadata.json alongside copied files.
    Runs as worker user to avoid permission issues (worktree owned by worker).
    No-op if worktree is absent or .switchboard/ doesn't exist.
    """
    worktree = task.get("worktree_path")
    if not worktree:
        return None

    src = Path(worktree) / ".switchboard"
    if not src.exists():
        return None

    slug = _task_slug(task["id"])
    dispatch_count = task.get("dispatch_count") or 1
    dest = Path(project["working_dir"]) / ".task-history" / slug / f"attempt-{dispatch_count}"

    try:
        # Create dest dir as worker user (project working_dir owned by worker)
        await _run_as_worker("mkdir", "-p", str(dest))

        # Copy each file as worker user
        for src_file in src.iterdir():
            if src_file.is_file():
                await _run_as_worker("cp", "-p", str(src_file), str(dest / src_file.name))

        metadata = {
            "task_id": task["id"],
            "attempt": dispatch_count,
            "reason": reason,
            "session_id": task.get("session_id"),
            "cost_usd": task.get("total_cost_usd"),
            "input_tokens": task.get("total_input_tokens"),
            "output_tokens": task.get("total_output_tokens"),
            "archived_at": db.now_iso(),
        }
        metadata_json = json.dumps(metadata, indent=2)
        # Write metadata via temp file + move (avoids stdin piping)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tmp:
            tmp.write(metadata_json)
            tmp_path = tmp.name
        await _run_as_worker("mv", tmp_path, str(dest / "metadata.json"))
        log.info(f"Archived logs for {task['id']} attempt {dispatch_count} to {dest} (reason={reason})")
        return dest
    except Exception as e:
        log.warning(f"archive_task_logs failed for {task['id']}: {e}")
        return None


async def list_attempts(task_id: str) -> dict:
    """List archived attempt folders for a task."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    project = await db.get_project(task["project_id"])
    if not project:
        raise ValueError(f"Project '{task['project_id']}' not found")

    slug = _task_slug(task_id)
    history_dir = Path(project["working_dir"]) / ".task-history" / slug

    if not history_dir.exists():
        return {"task_id": task_id, "attempts": []}

    attempts = []
    for attempt_dir in sorted(history_dir.iterdir()):
        if not attempt_dir.is_dir() or not attempt_dir.name.startswith("attempt-"):
            continue
        meta_path = attempt_dir / "metadata.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                meta = {}
        else:
            try:
                meta = {"attempt": int(attempt_dir.name.split("-")[1])}
            except (IndexError, ValueError):
                meta = {}
        meta["files"] = sorted(f.name for f in attempt_dir.iterdir() if f.is_file())
        attempts.append(meta)

    attempts.sort(key=lambda a: a.get("attempt", 0))
    return {"task_id": task_id, "attempts": attempts}


def _find_archive_path(project: dict, task_id: str, attempt: int | None) -> Path | None:
    """Resolve the archive dir for a task attempt. If attempt is None, returns highest-numbered."""
    slug = _task_slug(task_id)
    history_dir = Path(project["working_dir"]) / ".task-history" / slug
    if not history_dir.exists():
        return None
    if attempt is not None:
        p = history_dir / f"attempt-{attempt}"
        return p if p.exists() else None
    # Find highest-numbered attempt
    candidates = sorted(
        (d for d in history_dir.iterdir() if d.is_dir() and d.name.startswith("attempt-")),
        key=lambda d: int(d.name.split("-")[1]) if d.name.split("-")[1].isdigit() else 0,
    )
    return candidates[-1] if candidates else None


# ---------------------------------------------------------------------------
# Worktree Lifecycle
# ---------------------------------------------------------------------------

async def release_worktree(task_id: str, reason: str = "detach") -> dict:
    """Detach worktree without closing the task. Branch stays on origin."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    worktree = task.get("worktree_path")
    if not worktree:
        return {"task_id": task_id, "released": False, "reason": "No worktree attached"}

    project = await db.get_project(task["project_id"])

    # Archive logs before destroying the worktree
    if project:
        await archive_task_logs(task, project, reason)

    if project:
        bare_path = os.path.join(project["working_dir"], ".bare")
        if os.path.exists(bare_path) and os.path.exists(worktree):
            _, stderr, rc = await _run_as_worker(
                "git", "-C", bare_path, "worktree", "remove", "--force", worktree,
            )
            if rc != 0:
                log.warning(f"Worktree remove failed for {task_id}: {stderr.decode()}")
            else:
                log.info(f"Released worktree for {task_id}: {worktree}")

            # Clean up local branch ref so it doesn't block checkout from other worktrees
            branch = task.get("branch")
            if branch:
                _, stderr, rc = await _run_as_worker(
                    "git", "-C", bare_path, "branch", "-D", branch,
                )
                if rc == 0:
                    log.info(f"Deleted local branch ref {branch} for {task_id}")
                else:
                    log.warning(f"Failed to delete branch ref {branch}: {stderr.decode().strip()}")

    await db.update_task(task_id, worktree_path=None)
    return {"task_id": task_id, "released": True, "worktree_path": worktree}


async def _auto_release_worktree(task_id: str) -> None:
    """Release worktree after gate pass if auto_release_worktree is enabled."""
    task = await db.get_task(task_id)
    if not task:
        return

    # Resolve effective auto_release_worktree (default True)
    auto_release = task.get("auto_release_worktree")
    if auto_release is None:
        auto_release = True
    if not auto_release:
        return

    if not task.get("worktree_path"):
        return

    log.info(f"Auto-releasing worktree for {task_id}")
    await release_worktree(task_id, reason="completion")


# ---------------------------------------------------------------------------
# Public Task Operations
# ---------------------------------------------------------------------------

async def dispatch_task(
    project_id: str, task_id: str, goal: str,
    spec: str | None = None, checklist: list[str] | None = None,
    phase: str = "analysis", max_turns: int | None = None,
    max_wall_clock: int | None = None,
    escalation_criteria: str | None = None,
    review_feedback: list[dict] | None = None,
    branch: str | None = None,
    jira_ticket: str | None = None,
    conversation_id: str | None = None,
    model: str | None = None,
    auto_test: bool | None = None,
    depends_on: str | None = None,
    auto_review: bool | None = None,
    review_model: str | None = None,
    parent_task_id: str | None = None,
    auto_pr: bool | None = None,
    component_id: str | None = None,
    claude_chat_url: str | None = None,
    auto_merge: bool | None = None,
    auto_release_worktree: bool | None = None,
    max_test_retries: int | None = None,
    max_review_retries: int | None = None,
    base_branch: str | None = None,
    held: bool = False,
    created_by: int | None = None,
    dispatched_by: int | None = None,
) -> dict:
    """Create task (if needed), setup worktree, launch CC via Agent SDK.

    If concurrency limit is reached, the task is queued (FIFO) and dispatched
    automatically when a slot opens up.

    If held=True, the task is created but NOT dispatched — it stays in 'ready'
    status until manually approved.
    """

    # Validate mutual exclusion: auto_merge and auto_pr
    if auto_merge and auto_pr:
        raise ValueError("auto_merge and auto_pr are mutually exclusive. Set only one.")

    # Get project
    project = await db.get_project(project_id)
    if not project:
        raise ValueError(f"Project '{project_id}' not found. Register it with create_project first.")

    # Check if project or component is paused
    if project.get("paused"):
        raise ValueError(f"Project '{project_id}' is paused. Resume it before dispatching tasks.")
    if component_id:
        comp = await db.get_component(component_id)
        if comp and comp.get("paused"):
            raise ValueError(f"Component '{component_id}' is paused. Resume it before dispatching tasks.")

    # Resolve config: task param → project default → system default.
    # Applied before create_task so the DB stores the resolved values; gate logic
    # reads task fields directly (e.g. task.get("auto_test")) and must find them set.
    from switchboard.config.constants import SYSTEM_DEFAULTS
    resolved_auto_test = _resolve_limit(auto_test, project.get("auto_test"), SYSTEM_DEFAULTS["auto_test"])
    resolved_auto_review = _resolve_limit(auto_review, project.get("auto_review"), SYSTEM_DEFAULTS["auto_review"])
    resolved_auto_pr = _resolve_limit(auto_pr, project.get("auto_pr"), SYSTEM_DEFAULTS["auto_pr"])
    resolved_auto_merge = _resolve_limit(auto_merge, project.get("auto_merge"), SYSTEM_DEFAULTS["auto_merge"])
    resolved_review_model = _resolve_limit(review_model, project.get("review_model"), SYSTEM_DEFAULTS["review_model"])
    resolved_auto_release = _resolve_limit(auto_release_worktree, project.get("auto_release_worktree"), SYSTEM_DEFAULTS["auto_release_worktree"])
    resolved_max_test_retries = _resolve_limit(max_test_retries, project.get("max_test_retries"), SYSTEM_DEFAULTS["max_test_retries"])
    resolved_max_review_retries = _resolve_limit(max_review_retries, project.get("max_review_retries"), SYSTEM_DEFAULTS["max_review_retries"])

    # Create or get task
    task = await db.get_task(task_id)
    is_resume = False

    if task is None:
        task = await db.create_task(
            id=task_id, project_id=project_id, goal=goal,
            branch=branch,
            max_turns=max_turns, max_wall_clock=max_wall_clock,
            jira_ticket=jira_ticket, conversation_id=conversation_id,
            model=model, auto_test=resolved_auto_test, depends_on=depends_on,
            auto_review=resolved_auto_review, review_model=resolved_review_model,
            parent_task_id=parent_task_id, auto_pr=resolved_auto_pr,
            component_id=component_id, claude_chat_url=claude_chat_url,
            auto_merge=resolved_auto_merge, auto_release_worktree=resolved_auto_release,
            max_test_retries=resolved_max_test_retries, max_review_retries=resolved_max_review_retries,
            base_branch=base_branch,
            created_by=created_by, dispatched_by=dispatched_by,
        )
        if spec:
            await db.post_task_message(
                task_id=task_id, author="dispatcher", content=spec,
                type="spec", title="Task Spec", pinned=True,
            )
        if checklist:
            await db.create_checklist_items(task_id, checklist)

        # Persist held flag BEFORE dependency check — the depends_on branch
        # returns early, so held must be saved to DB here or it's silently dropped.
        if held:
            await db.update_task(task_id, held=True)
            task["held"] = True

        # Backward trigger: if depends_on parent hasn't passed gate yet, don't dispatch
        if depends_on:
            parent = await db.get_task(depends_on)
            if parent and not parent.get("gate_passed_at"):
                log.info(f"Task {task_id} waiting on parent {depends_on}")
                result = {
                    "task_id": task_id, "status": "ready",
                    "waiting_on": depends_on,
                    "branch": task["branch"],
                    "queued": False,
                }
                if task.get("held"):
                    result["held"] = True
                return result
    elif task["status"] == "cancelled":
        raise ValueError(
            f"Task '{task_id}' was previously cancelled. Use a new task ID, "
            f"or use retry_task to explicitly revive it."
        )
    elif task["status"] in ("needs-review", "turns-exhausted", "completed", "merged"):
        is_resume = True
        # Update depends_on if caller provided a new value (fixes stale prefix issue)
        if depends_on and task.get("depends_on") != depends_on:
            await db.update_task(task_id, depends_on=depends_on)
            task["depends_on"] = depends_on
    elif task["status"] == "working":
        raise RuntimeError(f"Task '{task_id}' is already running")

    # If held, set the flag and return without dispatching
    if held and not task.get("held"):
        await db.update_task(task_id, held=True)
        task["held"] = True
    if task.get("held") and not is_resume:
        log.info(f"Task {task_id} is held — requires approval before dispatch")
        return {
            "task_id": task_id, "status": "ready",
            "held": True,
            "branch": task.get("branch"),
            "queued": False,
        }

    # Check concurrency limit — queue if full (FIFO)
    active = await db.count_active_tasks()
    if active >= db.DEFAULT_MAX_CONCURRENT and not is_resume:
        queued_at = db.now_iso()
        await db.update_task(task_id, queued_at=queued_at)
        log.info(f"Task {task_id} queued (concurrency full: {active}/{db.DEFAULT_MAX_CONCURRENT})")
        return {
            "task_id": task_id, "status": "ready",
            "branch": task["branch"],
            "queued": True,
            "queued_at": queued_at,
        }

    # Setup worktree — dir_name is always filesystem-safe (no slashes)
    # Branch may contain slashes (e.g. feature/foo)
    short_name = task_id.split("/")[-1] if "/" in task_id else task_id
    effective_branch = task["branch"] or short_name
    if task["branch"] != effective_branch:
        await db.update_task(task_id, branch=effective_branch)
    worktree_path = await setup_worktree(project, short_name, effective_branch,
                                         depends_on=task.get("depends_on"),
                                         base_branch=task.get("base_branch"))

    # Configure credential helper so CC's direct git pushes use PAT auth
    await setup_credential_helper(worktree_path, project_id)

    # Run setup command
    await run_setup_command(project, worktree_path)

    # Setup logging
    log_dir = await _setup_log_dir(worktree_path)

    # Resolve limits and model
    effective_max_turns = _resolve_limit(
        task.get("max_turns"), project.get("max_turns"), db.DEFAULT_MAX_TURNS
    )
    effective_max_wall_clock = _resolve_limit(
        task.get("max_wall_clock"), project.get("max_wall_clock"), db.DEFAULT_MAX_WALL_CLOCK
    )
    effective_model = _resolve_limit(
        task.get("model"), project.get("model"), DEFAULT_MODEL
    )

    # Build prompt
    spec_content = None
    pinned = await db.get_task_pinned(task_id)
    if pinned:
        spec_content = pinned["content"]

    # Fetch checklist items with IDs so CC knows how to update them
    checklist_items = await db.get_checklist(task_id)

    prompt = await _build_task_prompt(project, task, spec_content, checklist_items, escalation_criteria, review_feedback)

    # Get session_id for resume
    session_id = task.get("session_id") if is_resume else None

    # Update task record
    dispatch_count = (task.get("dispatch_count") or 0) + 1
    await db.update_task(
        task_id,
        status="working",
        phase=phase,
        worktree_path=worktree_path,
        dispatch_count=dispatch_count,
        last_activity=db.now_iso(),
    )

    # Log dispatch
    _write_dispatch_log(
        log_dir, task_id, session_id or "(new)",
        effective_max_turns, effective_max_wall_clock,
        worktree_path, is_resume, effective_model,
    )

    # Launch SDK session in background — non-blocking
    task_handle = asyncio.create_task(
        _run_sdk_session(
            task_id=task_id,
            prompt=prompt,
            worktree_path=worktree_path,
            session_id=session_id,
            is_resume=is_resume,
            max_turns=effective_max_turns,
            max_wall_clock_minutes=effective_max_wall_clock,
            log_dir=log_dir,
            model=effective_model,
        ),
        name=f"sdk-session-{task_id}",
    )
    _running_tasks.add(task_handle)
    task_handle.add_done_callback(_handle_task_exception)

    # Notify Slack
    checklist_items = checklist_items or []
    await notify.task_dispatched(
        task_id=task_id, goal=goal, project_id=project_id,
        checklist_total=len(checklist_items),
        checklist=checklist_items,
        spec=spec_content,
        resumed=is_resume,
    )

    # Clear queued_at since we've dispatched
    if task.get("queued_at"):
        await db.update_task(task_id, queued_at=None)

    return {
        "task_id": task_id,
        "status": "working",
        "phase": phase,
        "worktree_path": worktree_path,
        "branch": effective_branch,
        "session_id": session_id,
        "dispatch_count": dispatch_count,
        "max_turns": effective_max_turns,
        "max_wall_clock": effective_max_wall_clock,
        "model": effective_model,
        "resumed": is_resume,
        "queued": False,
    }


async def resume_task(task_id: str, reset_recovery_count: bool = True) -> dict:
    """Resume a paused task with the same session ID.

    If worktree was auto-released, it will be re-attached automatically
    by setup_worktree() in dispatch_task().

    If the task already passed the gate, re-triggers the post-gate pipeline
    (auto-merge, chain advancement) instead of launching a new CC session.

    reset_recovery_count: set False when called from auto-recovery so the
    recovery_count increment is preserved.
    """
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")
    resumable = ("needs-review", "turns-exhausted", "completed", "merged", "rate-limited")
    if task["status"] not in resumable:
        raise ValueError(f"Task '{task_id}' is in status '{task['status']}', expected one of: {', '.join(resumable)}")

    # If gate already passed AND task is in a terminal state (not needs-review),
    # re-trigger post-gate pipeline instead of launching a new CC session.
    # Exceptions: needs-review or pr_status=conflict mean CC still has work to do.
    if (task.get("gate_passed_at")
            and task["status"] in ("completed", "merged")
            and task.get("pr_status") != "conflict"):
        log.info(f"Resume {task_id}: gate already passed, re-triggering post-gate pipeline")
        await _check_and_dispatch_dependents(task_id)
        return await db.get_task(task_id)

    # Clear stale pr_status; optionally reset recovery_count (skip for auto-recovery
    # so the increment from recover_orphaned_tasks is preserved for flap detection)
    updates = {}
    if task.get("pr_status"):
        updates["pr_status"] = None
    if reset_recovery_count and task.get("recovery_count"):
        updates["recovery_count"] = 0
    if updates:
        await db.update_task(task_id, **updates)

    return await dispatch_task(
        project_id=task["project_id"],
        task_id=task_id,
        goal=task["goal"],
        phase=task.get("phase") or "implementing",
    )


async def retry_task(task_id: str, clean: bool = False) -> dict:
    """Start a fresh session. Optionally clean worktree.

    If review/feedback messages were posted after the last CC result,
    they are injected into the prompt so CC knows to apply revisions.
    """
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    # Archive current attempt's logs before overwriting on next dispatch
    project = await db.get_project(task["project_id"])
    if project:
        await archive_task_logs(task, project, "retry")

    # Revert any punchlist items claimed by this task back to 'open'
    reverted = await db.revert_punchlist_items_for_task(task_id)
    if reverted:
        log.info(f"Task {task_id}: reverted {reverted} punchlist item(s) on retry")

    # Clear session and gate state to force fresh run through the pipeline
    # Increment current_attempt — this is a new attempt, not a resume
    # Also clear held flag so retried tasks dispatch normally
    new_attempt = (task.get("current_attempt") or 1) + 1
    await db.update_task(task_id, session_id=None, gate_status=None, gate_passed_at=None,
                         current_attempt=new_attempt, held=False)

    # Post "Attempt N starting..." so the attempt group appears in Foreman immediately
    # (before CC posts anything). Must happen after update_task so attempt_number is correct.
    await db.post_task_message(
        task_id=task_id, author="switchboard", type="status",
        title=f"Attempt {new_attempt} starting",
        content=f"Attempt {new_attempt} starting — fresh session launched.",
    )

    # Invalidate downstream chain if this task has dependents
    dependents = await db.get_dependents(task_id)
    if dependents:
        await _invalidate_chain(task_id)

    # Optionally clean worktree
    if clean and task.get("worktree_path") and os.path.exists(task["worktree_path"]):
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", task["worktree_path"], "checkout", ".",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    # Find review feedback posted after the last CC result message.
    # These are messages the user posted after task completion — CC needs
    # to treat them as revision instructions, not just context.
    review_feedback = None
    thread = await db.read_task_messages(task_id)
    messages = thread.get("messages", [])
    last_result_idx = None
    for i, msg in enumerate(messages):
        if msg.get("author") == "cc-worker" and msg.get("type") == "result":
            last_result_idx = i
    if last_result_idx is not None:
        feedback = [
            m for m in messages[last_result_idx + 1:]
            if m.get("author") != "dispatcher"  # Skip system status messages
        ]
        if feedback:
            review_feedback = feedback

    try:
        return await dispatch_task(
            project_id=task["project_id"],
            task_id=task_id,
            goal=task["goal"],
            phase="revisions" if review_feedback else "analysis",
            review_feedback=review_feedback,
        )
    except Exception as dispatch_err:
        # Dispatch failed after we already incremented current_attempt and posted
        # "Attempt N starting". Roll the task back to a retryable state so it
        # surfaces as attention-needed rather than silently becoming a ghost attempt.
        log.error(f"retry_task: dispatch failed for {task_id} (attempt {new_attempt}): {dispatch_err}")
        await db.update_task(task_id, status="needs-review")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Auto-retry dispatch failed",
            content=(
                f"Failed to dispatch attempt {new_attempt}: {dispatch_err}\n\n"
                f"Manual retry needed."
            ),
        )
        await notify.task_needs_review(
            task_id=task_id,
            reason=f"Auto-retry dispatch failed: {dispatch_err}",
        )
        return {"task_id": task_id, "status": "needs-review", "error": str(dispatch_err)}


async def reopen_task(task_id: str) -> dict:
    """Reopen a completed task for revisions.

    Increments current_attempt, sets status to 'reopened', clears session/gate state,
    and posts a status message. Does NOT dispatch or touch git — that's start_reopened_task's job.
    """
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")
    if task.get("status") != "completed":
        raise ValueError(f"Task '{task_id}' must be 'completed' to reopen (current: {task.get('status')})")

    new_attempt = (task.get("current_attempt") or 1) + 1
    await db.update_task(
        task_id,
        status="reopened",
        current_attempt=new_attempt,
        session_id=None,
        gate_status=None,
        gate_passed_at=None,
        reopen_saved_gate_status=task.get("gate_status"),
        reopen_saved_gate_passed_at=task.get("gate_passed_at"),
    )

    # Post status message — auto-stamped to new_attempt since current_attempt is now updated
    await db.post_task_message(
        task_id=task_id, author="switchboard", type="status",
        title="Task reopened — awaiting feedback",
        content="Task reopened for revisions. Post feedback below, then click Start.",
    )

    return await db.get_task(task_id)


async def cancel_reopen(task_id: str) -> dict:
    """Cancel a re-open — return the task to 'completed' status.

    Only callable on 'reopened' tasks. Decrements current_attempt back to the
    previous value and deletes the messages posted during the reopened state
    (the status message and any feedback notes).
    """
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")
    if task.get("status") != "reopened":
        raise ValueError(f"Task '{task_id}' must be 'reopened' to cancel re-open (current: {task.get('status')})")

    current_attempt = task.get("current_attempt") or 1
    prev_attempt = max(1, current_attempt - 1)

    # Delete messages stamped to the current (reopened) attempt
    async with db.get_db() as conn:
        await conn.execute(
            "DELETE FROM messages WHERE task_id = ? AND attempt_number = ?",
            (task_id, current_attempt),
        )
        await conn.commit()

    await db.update_task(
        task_id,
        status="completed",
        current_attempt=prev_attempt,
        gate_status=task.get("reopen_saved_gate_status"),
        gate_passed_at=task.get("reopen_saved_gate_passed_at"),
        reopen_saved_gate_status=None,
        reopen_saved_gate_passed_at=None,
    )

    return await db.get_task(task_id)


async def start_reopened_task(
    task_id: str,
    auto_test: bool | None = None,
    auto_review: bool | None = None,
) -> dict:
    """Start a reopened task — collect feedback, rebase, and dispatch.

    Only callable on 'reopened' tasks. Collects user messages posted since the
    reopen status message, posts 'Attempt N starting...', rebases onto base branch,
    invalidates chain dependents, then dispatches CC with the feedback as review_feedback.

    auto_test / auto_review override the task's defaults for this dispatch only.
    """
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")
    if task.get("status") != "reopened":
        raise ValueError(f"Task '{task_id}' must be 'reopened' to start (current: {task.get('status')})")

    current_attempt = task.get("current_attempt") or 1

    # Find feedback messages: everything posted by non-switchboard authors after the
    # reopen status message (first message stamped to current_attempt)
    thread = await db.read_task_messages(task_id)
    messages = thread.get("messages", [])

    # Find the index of the first message with the new attempt_number (the reopen status msg)
    reopen_msg_idx = None
    for i, msg in enumerate(messages):
        if (msg.get("attempt_number") or 1) == current_attempt:
            reopen_msg_idx = i
            break

    review_feedback = None
    if reopen_msg_idx is not None:
        feedback = [
            m for m in messages[reopen_msg_idx + 1:]
            if m.get("author") not in ("switchboard", "dispatcher", "cc-worker")
        ]
        if feedback:
            review_feedback = feedback

    # Post "Attempt N starting..." so the group appears in Foreman before CC posts anything
    await db.post_task_message(
        task_id=task_id, author="switchboard", type="status",
        title=f"Attempt {current_attempt} starting",
        content=f"Attempt {current_attempt} starting — revision session launched.",
    )

    # Rebase onto base branch before dispatch
    rebase_ok = await _sync_branch_with_base(task)
    if not rebase_ok:
        # Rebase conflict posted to thread — stop here so user can resolve
        return await db.get_task(task_id)

    # Invalidate downstream chain (deferred from reopen to here)
    dependents = await db.get_dependents(task_id)
    if dependents:
        await _invalidate_chain(task_id)

    # Build dispatch kwargs, applying per-dispatch overrides if provided
    dispatch_kwargs: dict = dict(
        project_id=task["project_id"],
        task_id=task_id,
        goal=task["goal"],
        phase="revisions",
        review_feedback=review_feedback,
    )
    if auto_test is not None:
        dispatch_kwargs["auto_test"] = auto_test
    if auto_review is not None:
        dispatch_kwargs["auto_review"] = auto_review

    result = await dispatch_task(**dispatch_kwargs)

    # Notify that a new attempt is starting
    await notify.task_attempt_starting(task_id, current_attempt, task["goal"])

    return result


async def cancel_task(task_id: str) -> dict:
    """Kill a running task — cancel the asyncio Task, then update DB status."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    # Find and cancel the running asyncio task
    cancelled_async = False
    task_name = f"sdk-session-{task_id}"
    for t in list(_running_tasks):
        if t.get_name() == task_name and not t.done():
            t.cancel()
            cancelled_async = True
            log.info(f"Cancelled asyncio task for {task_id}")
            break

    if not cancelled_async and task.get("status") == "working":
        log.warning(f"Could not find running asyncio task for {task_id} — it may have been lost on restart")

    await db.update_task(task_id, status="cancelled", held=False)

    # Revert any punchlist items claimed by this task back to 'open'
    reverted = await db.revert_punchlist_items_for_task(task_id)
    if reverted:
        log.info(f"Task {task_id}: reverted {reverted} punchlist item(s) on cancel")

    # A slot freed up — drain the FIFO queue
    await _drain_queue()

    return {"task_id": task_id, "status": "cancelled", "async_task_cancelled": cancelled_async}


async def skip_gate(task_id: str) -> dict:
    """Manually bypass the test/review gate, marking it as passed."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")
    await db.update_task(task_id, gate_status="passed", gate_passed_at=db.now_iso())
    await db.post_task_message(
        task_id=task_id, author="dispatcher", type="status",
        title="Gate skipped",
        content="Gate manually bypassed by user.",
    )
    await _check_and_dispatch_dependents(task_id)
    return {"task_id": task_id, "gate_status": "passed"}


async def advance_chain(task_id: str) -> dict:
    """Manually dispatch next dependent task (bypasses first-pass check)."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")
    if not task.get("gate_passed_at"):
        raise ValueError(f"Task '{task_id}' gate has not passed yet")

    dependents = await db.get_dependents(task_id)
    dispatched = []
    for dep in dependents:
        if dep["status"] == "ready":
            try:
                await dispatch_task(
                    project_id=dep["project_id"],
                    task_id=dep["id"],
                    goal=dep["goal"],
                    auto_test=dep.get("auto_test", True),
                )
                dispatched.append(dep["id"])
            except Exception as e:
                log.error(f"Failed to advance chain to {dep['id']}: {e}")
    return {"task_id": task_id, "dispatched": dispatched}


async def cancel_chain(task_id: str) -> dict:
    """Cancel a task and all its dependents recursively."""
    cancelled = []

    async def _cancel_recursive(tid: str):
        task = await db.get_task(tid)
        if not task or task["status"] in ("cancelled", "completed"):
            return
        # Cancel running tasks
        if task["status"] == "working":
            await cancel_task(tid)
        else:
            await db.update_task(tid, status="cancelled")
        cancelled.append(tid)
        # Recurse into dependents
        deps = await db.get_dependents(tid)
        for dep in deps:
            await _cancel_recursive(dep["id"])

    await _cancel_recursive(task_id)
    return {"cancelled": cancelled}


async def approve_task(task_id: str) -> dict:
    """Release a held task for dispatch."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")
    if not task.get("held"):
        raise ValueError(f"Task '{task_id}' is not held")

    await db.update_task(task_id, held=False)
    log.info(f"Task {task_id} approved — releasing hold")

    await db.post_task_message(
        task_id=task_id, author="dispatcher", type="status",
        title="Approved",
        content="Task hold released. Dispatching.",
    )

    # Dispatch if the task is ready (dependencies met)
    if task["status"] == "ready":
        if task.get("depends_on"):
            parent = await db.get_task(task["depends_on"])
            if parent and not parent.get("gate_passed_at"):
                return {"task_id": task_id, "status": "ready", "held": False,
                        "waiting_on": task["depends_on"]}

        return await dispatch_task(
            project_id=task["project_id"],
            task_id=task_id,
            goal=task["goal"],
        )

    return await db.get_task(task_id)


async def close_task(task_id: str, cleanup: bool = True, force_delete_branch: bool = False) -> dict:
    """Manually close a task — no gates, no chain advancement, work ends here."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    if task["status"] == "working":
        raise ValueError(
            f"Task '{task_id}' is still running. Cancel it first, then close."
        )

    project = await db.get_project(task["project_id"])

    # Archive logs before destroying the worktree
    if project:
        await archive_task_logs(task, project, "close")

    if cleanup and project:
        await cleanup_worktree(project, task, force_delete_branch)
        await db.update_task(
            task_id, status="completed", worktree_path=None,
            gate_passed_at=None, held=False,
        )
    else:
        await db.update_task(
            task_id, status="completed",
            gate_passed_at=None, held=False,
        )

    # Post status message so it's clear this was a manual close
    await db.post_task_message(
        task_id=task_id, author="dispatcher", type="status",
        title="Manually closed",
        content="Task was manually closed — no gates or chain actions triggered.",
    )

    return {"task_id": task_id, "status": "completed", "cleaned_up": cleanup, "manually_closed": True}


# ---------------------------------------------------------------------------
# Component / Project Pause & Stop
# ---------------------------------------------------------------------------

async def pause_component(component_id: str) -> dict:
    """Pause a component — no new tasks will be dispatched."""
    comp = await db.get_component(component_id)
    if not comp:
        raise ValueError(f"Component '{component_id}' not found")
    await db.update_component(component_id, paused=True)
    log.info(f"Component {component_id} paused")
    return {"component_id": component_id, "paused": True}


async def resume_component(component_id: str) -> dict:
    """Resume a paused component — tasks can be dispatched again."""
    comp = await db.get_component(component_id)
    if not comp:
        raise ValueError(f"Component '{component_id}' not found")
    await db.update_component(component_id, paused=False)
    log.info(f"Component {component_id} resumed")
    return {"component_id": component_id, "paused": False}


async def stop_component(component_id: str) -> dict:
    """Stop a component — pause + cancel all running tasks."""
    comp = await db.get_component(component_id)
    if not comp:
        raise ValueError(f"Component '{component_id}' not found")
    await db.update_component(component_id, paused=True)
    # Cancel all working tasks in this component
    all_tasks = await db.list_tasks(status="working")
    cancelled = []
    for task in all_tasks:
        if task.get("component_id") == component_id:
            try:
                await cancel_task(task["id"])
                cancelled.append(task["id"])
            except Exception as e:
                log.warning(f"Failed to cancel {task['id']} during component stop: {e}")
    log.info(f"Component {component_id} stopped, cancelled {len(cancelled)} tasks")
    return {"component_id": component_id, "paused": True, "cancelled": cancelled}


async def pause_project(project_id: str) -> dict:
    """Pause a project — no new tasks will be dispatched."""
    project = await db.get_project(project_id)
    if not project:
        raise ValueError(f"Project '{project_id}' not found")
    await db.update_project(project_id, paused=True)
    log.info(f"Project {project_id} paused")
    return {"project_id": project_id, "paused": True}


async def resume_project(project_id: str) -> dict:
    """Resume a paused project — tasks can be dispatched again."""
    project = await db.get_project(project_id)
    if not project:
        raise ValueError(f"Project '{project_id}' not found")
    await db.update_project(project_id, paused=False)
    log.info(f"Project {project_id} resumed")
    return {"project_id": project_id, "paused": False}


async def stop_project(project_id: str) -> dict:
    """Stop a project — pause + cancel all running tasks."""
    project = await db.get_project(project_id)
    if not project:
        raise ValueError(f"Project '{project_id}' not found")
    await db.update_project(project_id, paused=True)
    all_tasks = await db.list_tasks(status="working")
    cancelled = []
    for task in all_tasks:
        if task.get("project_id") == project_id:
            try:
                await cancel_task(task["id"])
                cancelled.append(task["id"])
            except Exception as e:
                log.warning(f"Failed to cancel {task['id']} during project stop: {e}")
    log.info(f"Project {project_id} stopped, cancelled {len(cancelled)} tasks")
    return {"project_id": project_id, "paused": True, "cancelled": cancelled}
