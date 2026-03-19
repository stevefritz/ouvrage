"""Task execution engine — Agent SDK dispatch, worktree ops, lifecycle management."""

import asyncio
import json
import logging
import os
import pwd
import shlex
import shutil
import time
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anyio

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    UserMessage,
)
from claude_agent_sdk.types import TextBlock, ToolUseBlock, ToolResultBlock

import database as db
import notifications as notify

# ---------------------------------------------------------------------------
# Process group isolation — patch anyio.open_process at module load time
# ---------------------------------------------------------------------------
# CC workers run via the Agent SDK, which uses anyio.open_process internally.
# Without start_new_session=True, CC and Switchboard share a process group.
# If CC runs `kill -PGID` (e.g., trying to clean up hung tests), the signal
# can propagate up and terminate the Switchboard process itself. This has
# happened multiple times in production.
#
# By forcing start_new_session=True on every SDK subprocess spawn, CC gets
# its own session and process group — signals within CC's group can't escape
# upward to Switchboard.
#
# Both tasks.py and the SDK transport module reference the same anyio module
# object, so patching anyio.open_process here affects all SDK subprocess spawns.
_orig_anyio_open_process = anyio.open_process


async def _isolated_open_process(command, *, start_new_session: bool = False, **kwargs):
    """Wrapper that forces start_new_session=True for all subprocess spawns."""
    return await _orig_anyio_open_process(command, start_new_session=True, **kwargs)


anyio.open_process = _isolated_open_process

log = logging.getLogger("switchboard.tasks")

# Track running async tasks to prevent garbage collection and silent failures
_running_tasks: set[asyncio.Task] = set()

# Track active SDK clients for tasks (used by cancel to interrupt)
_active_clients: dict[str, ClaudeSDKClient] = {}

MESSAGE_POLL_INTERVAL = 5  # seconds between DB polls for injected messages
DEFAULT_MODEL = "sonnet"


def _handle_task_exception(task: asyncio.Task) -> None:
    """Log unhandled exceptions from background tasks and clean up tracking."""
    _running_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.error(f"Background task {task.get_name()} failed: {exc}", exc_info=exc)


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is running by PID."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _resolve_limit(task_val, project_val, global_default):
    """Resolve a limit: task override > project default > global default."""
    if task_val is not None:
        return task_val
    if project_val is not None:
        return project_val
    return global_default


# ---------------------------------------------------------------------------
# Crash Recovery Configuration
# ---------------------------------------------------------------------------

RECOVERY_STAGGER_SECONDS = int(os.environ.get("RECOVERY_STAGGER_SECONDS", "30"))
MAX_RECOVERY_ATTEMPTS = int(os.environ.get("MAX_RECOVERY_ATTEMPTS", "3"))
RECOVERY_ENABLED = os.environ.get("RECOVERY_ENABLED", "true").lower() in ("true", "1", "yes")


async def mark_working_for_recovery():
    """Called during graceful shutdown — flag all working tasks for auto-recovery.

    Sets recovery_priority so startup recovery knows these were killed by SIGTERM,
    not by a real failure. This runs in the lifespan.shutdown handler before the
    event loop dies.
    """
    working = await db.list_tasks(status="working")
    for task in working:
        await db.update_task(task["id"], recovery_priority=True)
        log.info(f"Shutdown: marked {task['id']} for recovery")


def _classify_orphan(task: dict) -> tuple[int, str]:
    """Classify an orphaned task for recovery priority and method.

    Returns (priority, method) where lower priority = dispatched first.
    Priority:
      0 = gate subtask (unblock parent)
      1 = chain parent with waiting dependents
      2 = has session_id (resume)
      3 = no session_id (retry)
    Method: 'gate_subtask', 'resume', 'retry'
    """
    # Gate subtask: has parent_task_id → re-trigger parent gate
    if task.get("parent_task_id"):
        return (0, "gate_subtask")

    # Has session_id → resumable
    if task.get("session_id"):
        return (2, "resume")

    # No session_id → retry
    return (3, "retry")


async def _classify_with_dependents(task: dict) -> tuple[int, str]:
    """Like _classify_orphan but checks for waiting dependents (async DB query)."""
    priority, method = _classify_orphan(task)

    # Upgrade priority if this task has waiting dependents (chain parent)
    if priority > 1 and not task.get("parent_task_id"):
        dependents = await db.get_dependents(task["id"])
        if any(d["status"] == "ready" for d in dependents):
            return (1, method)

    return (priority, method)


async def _verify_worktree(task: dict) -> bool:
    """Check if a task's worktree exists, looks valid, and is clean."""
    worktree = task.get("worktree_path")
    if not worktree or not os.path.exists(worktree):
        return False
    # Check for .git file/dir (worktree marker)
    if not os.path.exists(os.path.join(worktree, ".git")):
        return False
    # Verify worktree is clean and not corrupted
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            cwd=worktree,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            return False  # corrupted worktree
        # Dirty worktree is expected for SIGTERM'd tasks — CC will resume
        # into whatever state was left. Only reject truly corrupted worktrees.
        return True
    except Exception:
        return False


async def _build_recovery_message(task: dict, method: str, position: int, total: int,
                                   recovery_count: int) -> str:
    """Build a status message for a recovered task."""
    session_id = task.get("session_id") or "(none)"
    phase = task.get("phase") or "unknown"

    # Get checklist progress
    checklist = await db.get_checklist(task["id"])
    done = sum(1 for item in checklist if item.get("done"))
    checklist_total = len(checklist)

    method_label = {
        "resume": "resume (session preserved)",
        "retry": "retry (fresh session)",
        "gate_subtask": "re-trigger parent gate",
    }.get(method, method)

    return (
        f"⚡ Service restart detected — auto-resuming session {session_id}\n"
        f"  Previous state: working (phase: {phase}, {done}/{checklist_total} checklist)\n"
        f"  Recovery method: {method_label}\n"
        f"  Recovery attempt: {recovery_count}\n"
        f"  Stagger position: {position} of {total} tasks recovering"
    )


async def recover_orphaned_tasks():
    """Recover tasks left in broken states after a service restart.

    Handles three categories:
    1. Orphaned CC sessions (status=working, no live PID) → auto-resume or retry
    2. Gate subtasks (parent_task_id set) → re-trigger parent gate
    3. Gate pipeline stuck (test-failed/review-failed) → re-trigger gate

    Features staggered recovery, flap detection, and FIFO queue priority.
    """
    # Re-trigger gate for tasks stuck in test-failed/review-failed
    # (this is independent of RECOVERY_ENABLED — it's existing behavior)
    all_tasks = await db.list_tasks(status="completed")
    for task in all_tasks:
        gate = task.get("gate_status")
        if gate in ("test-failed", "review-failed"):
            log.warning(f"Startup recovery: task {task['id']} has gate_status={gate}, re-triggering gate")
            project = await db.get_project(task["project_id"])
            if not project:
                continue
            if gate == "test-failed" and task.get("auto_test") and project.get("test_command"):
                await _run_test_gate(task["id"], project, task)
            elif gate == "review-failed" and task.get("auto_review"):
                await _dispatch_review(task["id"], project, task)

    # Find orphaned working tasks
    working_tasks = await db.list_tasks(status="working")
    orphans = []
    for task in working_tasks:
        pid = task.get("pid")
        if pid and _is_pid_alive(pid):
            continue  # still running
        orphans.append(task)

    # Layer 3: Find silently killed tasks — failed with no worker messages
    # (only spec/status messages = CC never got a chance to run)
    failed_tasks = await db.list_tasks(status="failed")
    for task in failed_tasks:
        thread = await db.read_task_messages(task["id"])
        messages = thread.get("messages", [])
        # Check if the task died from a signal (SIGTERM/SIGKILL) or silently
        last_msg = messages[-1] if messages else {}
        last_content = last_msg.get("content", "")
        killed_by_signal = any(s in last_content for s in ("exit code -15", "exit code -9", "exit code 143", "exit code 137"))
        has_worker_output = any(m.get("author") == "cc-worker" for m in messages)

        if killed_by_signal or not has_worker_output:
            reason = "killed by signal" if killed_by_signal else "no worker output"
            log.warning(f"Startup recovery: task {task['id']} failed ({reason}), treating as orphan")
            await db.update_task(task["id"], status="working")
            orphans.append(task)

    if not orphans:
        return

    # Mark all orphans as needs-review immediately so they don't count against
    # concurrency while we process them one by one with stagger delays
    for task in orphans:
        await db.update_task(task["id"], status="needs-review")

    # If recovery is disabled, just post messages (already marked needs-review above)
    if not RECOVERY_ENABLED:
        for task in orphans:
            log.warning(f"Startup recovery (disabled): task {task['id']} marked needs-review")
            await db.post_task_message(
                task_id=task["id"], author="dispatcher", type="status",
                title="Recovered after restart",
                content="Service restarted while this task was running. "
                        "Auto-recovery is disabled. Marked as needs-review.",
            )
        return

    # Classify and sort orphans by priority
    classified: list[tuple[int, str, dict]] = []
    for task in orphans:
        priority, method = await _classify_with_dependents(task)
        classified.append((priority, method, task))
    classified.sort(key=lambda x: x[0])

    total = len(classified)
    log.info(f"Startup recovery: {total} orphaned tasks to recover")

    for position, (priority, method, task) in enumerate(classified, 1):
        task_id = task["id"]
        current_count = task.get("recovery_count") or 0

        # Flap detection — check BEFORE incrementing
        if current_count >= MAX_RECOVERY_ATTEMPTS:
            log.warning(f"Recovery limit reached for {task_id} ({current_count} attempts)")
            await db.update_task(task_id, status="needs-review")
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title="Recovery limit reached",
                content=f"Recovery limit reached ({current_count} attempts). "
                        "Manual intervention required.",
            )
            continue

        recovery_count = current_count + 1

        # Update recovery tracking
        await db.update_task(task_id,
                             recovery_count=recovery_count,
                             last_recovery_at=db.now_iso())

        # Post recovery status message
        msg = await _build_recovery_message(task, method, position, total, recovery_count)
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Auto-recovery initiated", content=msg,
        )

        # Stagger: first task dispatches immediately, subsequent tasks wait
        if position > 1:
            log.info(f"Recovery stagger: waiting {RECOVERY_STAGGER_SECONDS}s before {task_id} ({position}/{total})")
            await asyncio.sleep(RECOVERY_STAGGER_SECONDS)

        # Check concurrency before dispatching
        active = await db.count_active_tasks()
        if active >= db.DEFAULT_MAX_CONCURRENT:
            # Queue with recovery priority (front of FIFO queue)
            log.info(f"Recovery: queuing {task_id} with priority (concurrency full: {active}/{db.DEFAULT_MAX_CONCURRENT})")
            await db.update_task(task_id, status="ready",
                                 queued_at=db.now_iso(), recovery_priority=True)
            continue

        # Dispatch based on method
        try:
            await _recover_task(task_id, task, method)
        except Exception as e:
            log.error(f"Recovery failed for {task_id}: {e}", exc_info=True)
            await db.update_task(task_id, status="needs-review")
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title="Recovery failed",
                content=f"Auto-recovery failed:\n```\n{e}\n```\nMarked as needs-review.",
            )


async def _recover_task(task_id: str, task: dict, method: str) -> None:
    """Execute the recovery method for a single task."""
    if method == "gate_subtask":
        await _recover_gate_subtask(task_id, task)
    elif method == "resume":
        await _recover_with_resume(task_id, task)
    elif method == "retry":
        await _recover_with_retry(task_id, task)


async def _recover_gate_subtask(task_id: str, task: dict) -> None:
    """Re-trigger parent gate pipeline for an orphaned gate subtask."""
    parent_id = task["parent_task_id"]
    parent = await db.get_task(parent_id)
    if not parent:
        log.warning(f"Recovery: gate subtask {task_id} parent {parent_id} not found, falling back to needs-review")
        await db.update_task(task_id, status="needs-review")
        return

    project = await db.get_project(parent["project_id"])
    if not project:
        await db.update_task(task_id, status="needs-review")
        return

    # Cancel the orphaned subtask
    await db.update_task(task_id, status="cancelled")

    # Re-trigger the appropriate gate step on the parent
    gate = parent.get("gate_status")
    if gate in ("testing", "test-failed") and parent.get("auto_test") and project.get("test_command"):
        log.info(f"Recovery: re-triggering test gate for parent {parent_id}")
        await _run_test_gate(parent_id, project, parent)
    elif gate in ("reviewing", "review-failed") and parent.get("auto_review"):
        log.info(f"Recovery: re-triggering review for parent {parent_id}")
        await _dispatch_review(parent_id, project, parent)
    else:
        log.warning(f"Recovery: gate subtask {task_id} parent {parent_id} gate_status={gate}, cannot re-trigger")
        await db.update_task(task_id, status="needs-review")


async def _recover_with_resume(task_id: str, task: dict) -> None:
    """Resume a task with existing session_id. Falls back to retry on failure."""
    # Verify worktree before resume
    if not await _verify_worktree(task):
        log.warning(f"Recovery: worktree missing/corrupt for {task_id}, falling back to retry")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Worktree unavailable",
            content="Worktree missing or corrupted — falling back to fresh retry.",
        )
        await _recover_with_retry(task_id, task)
        return

    # Task is already in needs-review status (set at start of recovery)
    try:
        await resume_task(task_id, reset_recovery_count=False)
        log.info(f"Recovery: resumed {task_id} with session {task.get('session_id')}")
    except Exception as e:
        log.warning(f"Recovery: resume failed for {task_id}: {e}, falling back to retry")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Resume failed",
            content=f"Could not resume session: {e}\nFalling back to fresh retry.",
        )
        # Ensure task is in a retryable state
        await db.update_task(task_id, status="needs-review")
        await _recover_with_retry(task_id, task)


async def _recover_with_retry(task_id: str, task: dict) -> None:
    """Retry a task with a fresh session."""
    # Task is already in needs-review status (set at start of recovery)
    try:
        await retry_task(task_id)
        log.info(f"Recovery: retried {task_id} with fresh session")
    except Exception as e:
        log.warning(f"Recovery: retry failed for {task_id}: {e}")
        await db.update_task(task_id, status="needs-review")
        raise


STALL_THRESHOLD_SECONDS = 300  # 5 minutes
STALL_CHECK_INTERVAL = 60  # check every minute


async def check_stalled_tasks():
    """Background loop: check for working tasks with no recent activity or dead processes."""
    while True:
        try:
            await asyncio.sleep(STALL_CHECK_INTERVAL)
            working_tasks = await db.list_tasks(status="working")
            now = datetime.now(timezone.utc)
            for task in working_tasks:
                task_id = task["id"]
                has_active_client = task_id in _active_clients

                last_activity = task.get("last_activity")
                if not last_activity:
                    continue
                last = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                idle = (now - last).total_seconds()

                if has_active_client:
                    # Active SDK client — check for stall (long idle despite live connection)
                    if idle >= STALL_THRESHOLD_SECONDS:
                        thread = await db.read_task_messages(task["id"], last_n=5)
                        recent = thread.get("messages", [])
                        already_warned = any(
                            m.get("type") == "stall-warning" and m.get("author") == "dispatcher"
                            for m in recent
                        )
                        if not already_warned:
                            minutes = round(idle / 60, 1)
                            await db.post_task_message(
                                task_id=task["id"], author="dispatcher",
                                type="stall-warning",
                                title=f"No activity for {minutes}m",
                                content=f"Task has had no activity for {minutes} minutes. "
                                        "The CC session may be stuck or waiting for input.",
                            )
                            await notify.task_heartbeat(
                                task_id=task["id"], turns=0,
                                elapsed_s=idle,
                                last_tool=f"[STALL WARNING: {minutes}m idle]",
                            )
                            log.warning(f"Stall detected: task {task['id']} idle for {minutes}m")
                else:
                    # No active client — check if it's a real orphan
                    if idle > 120:  # 2 min with no active client = dead
                        log.warning(f"Health check: task {task_id} has no active SDK client and idle {idle:.0f}s, auto-recovering")
                        task_obj = await db.get_task(task_id)
                        if task_obj:
                            await db.update_task(task_id, recovery_priority=True)
                            await db.post_task_message(
                                task_id=task_id, author="dispatcher", type="status",
                                title="Orphaned task — auto-recovering",
                                content=f"Task has no active session and has been idle for {idle:.0f}s. "
                                        "Initiating auto-recovery.",
                            )
                            await _recover_single_task(task_obj)

            # Check for ready tasks whose parents have passed — missed chain advancement
            ready_tasks = await db.list_tasks(status="ready")
            for task in ready_tasks:
                if not task.get("depends_on"):
                    continue
                parent = await db.get_task(task["depends_on"])
                if parent and parent.get("gate_passed_at"):
                    # Skip if parent's auto-merge failed — chain shouldn't advance
                    if parent.get("auto_merge") and parent.get("pr_status") not in (None, "merged"):
                        continue
                    # Skip if project or component is paused
                    proj = await db.get_project(task["project_id"])
                    if proj and proj.get("paused"):
                        continue
                    if task.get("component_id"):
                        comp = await db.get_component(task["component_id"])
                        if comp and comp.get("paused"):
                            continue
                    log.warning(f"Health check: ready task {task['id']} has passed parent {parent['id']}, dispatching")
                    await db.post_task_message(
                        task_id=task["id"], author="dispatcher", type="status",
                        title="Chain advancement recovered",
                        content=f"Parent `{parent['id']}` passed but chain dispatch was missed. Auto-dispatching.",
                    )
                    try:
                        await retry_task(task["id"])
                    except Exception as e:
                        log.warning(f"Health check chain dispatch failed for {task['id']}: {e}")

            # Check for rate-limited tasks whose retry_after has passed
            all_tasks_for_retry = await db.list_tasks(status="rate-limited")
            for task in all_tasks_for_retry:
                retry_after = task.get("retry_after")
                if not retry_after:
                    continue
                retry_time = datetime.fromisoformat(retry_after.replace("Z", "+00:00"))
                if now >= retry_time:
                    log.info(f"Health check: rate-limited task {task['id']} retry_after has passed, dispatching")
                    await db.post_task_message(
                        task_id=task["id"], author="dispatcher", type="status",
                        title="Rate limit expired — auto-retrying",
                        content=f"Scheduled retry time ({retry_after}) has passed. Re-dispatching.",
                    )
                    await db.update_task(task["id"], retry_after=None)
                    try:
                        await retry_task(task["id"])
                    except Exception as e:
                        log.warning(f"Health check rate-limit retry failed for {task['id']}: {e}")

            # Also check any task with retry_after set (general purpose)
            # This lets us use retry_after for any backoff scenario
            for status in ("needs-review", "failed"):
                backoff_tasks = await db.list_tasks(status=status)
                for task in backoff_tasks:
                    retry_after = task.get("retry_after")
                    if not retry_after:
                        continue
                    retry_time = datetime.fromisoformat(retry_after.replace("Z", "+00:00"))
                    if now >= retry_time:
                        log.info(f"Health check: {status} task {task['id']} retry_after has passed, dispatching")
                        await db.update_task(task["id"], retry_after=None)
                        try:
                            await retry_task(task["id"])
                        except Exception as e:
                            log.warning(f"Health check retry_after dispatch failed for {task['id']}: {e}")

        except Exception as e:
            log.warning(f"Stall check error: {e}")


async def _recover_single_task(task: dict):
    """Attempt to recover a single dead/orphaned task."""
    task_id = task["id"]
    current_count = task.get("recovery_count") or 0

    if current_count >= MAX_RECOVERY_ATTEMPTS:
        log.warning(f"Recovery limit reached for {task_id} ({current_count} attempts)")
        await db.update_task(task_id, status="needs-review")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Recovery limit reached",
            content=f"Auto-recovery failed {current_count} times. Manual intervention required.",
        )
        return

    await db.update_task(task_id,
                         status="needs-review",
                         recovery_count=current_count + 1,
                         last_recovery_at=db.now_iso())

    try:
        if task.get("session_id"):
            log.info(f"Health check recovery: resuming {task_id}")
            await resume_task(task_id)
        else:
            log.info(f"Health check recovery: retrying {task_id}")
            await retry_task(task_id)
    except Exception as e:
        log.warning(f"Health check recovery failed for {task_id}: {e}")
        await db.update_task(task_id, status="needs-review")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Auto-recovery failed",
            content=f"Recovery attempt failed: {e}",
        )


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


def _tail_lines(text: str, max_chars: int) -> str:
    """Truncate text to last ~max_chars, breaking at line boundaries."""
    if len(text) <= max_chars:
        return text
    # Find the first newline after the cut point
    cut = len(text) - max_chars
    idx = text.find("\n", cut)
    if idx == -1:
        return text[cut:]
    return text[idx + 1:]


# ---------------------------------------------------------------------------
# Git Worktree Management
# ---------------------------------------------------------------------------

WORKER_USER = os.environ.get("WORKER_USER", "switchboard")


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

    # Fetch latest from remote
    await _run_as_worker("git", "-C", bare_path, "fetch", "origin")

    # Auto-detect default branch from bare clone HEAD if project config is wrong
    default_branch = project["default_branch"]
    stdout, _, _ = await _run_as_worker("git", "-C", bare_path, "symbolic-ref", "HEAD")
    detected = stdout.decode().strip().removeprefix("refs/heads/")
    if detected and detected != default_branch:
        log.info(f"Auto-detected default branch '{detected}' (project config said '{default_branch}')")
        default_branch = detected

    # Update local default branch ref to match origin BEFORE branching
    await _run_as_worker(
        "git", "-C", bare_path, "fetch", "origin",
        f"{default_branch}:{default_branch}",
    )

    # Branch chaining: if this task depends on another, branch from parent's branch
    base_branch = default_branch
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
        # Branch might already exist, try without -b
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


# ---------------------------------------------------------------------------
# Prompt Building
# ---------------------------------------------------------------------------

async def _build_task_prompt(project: dict, task: dict, spec_content: str | None,
                             checklist: list[dict] | None = None,
                             escalation_criteria: str | None = None,
                             review_feedback: list[dict] | None = None) -> str:
    """Build the prompt CC receives when dispatched."""
    parts = []

    # If this is a retry with review feedback, lead with that
    if review_feedback:
        parts.append("# ⚠️ REVISION REQUESTED")
        parts.append("")
        parts.append("This task was previously completed but needs revisions based on review feedback.")
        parts.append("**Your primary job is to address the feedback below.** The original spec is included")
        parts.append("for context, but focus on the reviewer's requested changes.")
        parts.append("")
        parts.append("## Review Feedback")
        for msg in review_feedback:
            author = msg.get("author", "reviewer")
            title = msg.get("title", "")
            header = f"### {title}" if title else f"### From {author}"
            parts.append(header)
            parts.append(msg.get("content", ""))
            parts.append("")

    # Context injection from parent task (dependency chain)
    if task.get("depends_on"):
        parent = await db.get_task(task["depends_on"])
        if parent:
            parts.append("## Prior Task Context")
            parts.append(f"Task `{parent['id']}` completed. Branch: `{parent['branch']}`")

            # Get parent's result message and handoff notes
            parent_msgs = await db.read_task_messages(parent["id"])
            for msg in reversed(parent_msgs.get("messages", [])):
                if msg.get("type") == "result" and msg.get("author") == "cc-worker":
                    parts.append(f"\n### Result\n{msg['content']}")
                    break
            for msg in reversed(parent_msgs.get("messages", [])):
                if msg.get("type") == "handoff":
                    parts.append(f"\n### Handoff Notes\n{msg['content']}")
                    break
            parts.append("")

    parts.append(f"# Task: {task['goal']}")
    parts.append(f"Project: {project['id']} | Branch: {task['branch']}")
    parts.append(f"Task ID: {task['id']}")
    parts.append("")

    if spec_content:
        parts.append("## Original Spec")
        parts.append(spec_content)
        parts.append("")

    if checklist:
        parts.append("## Checklist")
        parts.append("Mark items done as you complete them using `mcp__switchboard__update_task_checklist`.")
        parts.append("")
        for item in checklist:
            status = "✅" if item.get("done") else "⬜"
            parts.append(f"- {status} (item_id={item['id']}) {item['item']}")
        parts.append("")

    parts.append("## Instructions")
    parts.append("- You are working in an isolated git worktree. Commit freely to your branch.")
    parts.append("- Use the switchboard MCP tools to report progress:")
    parts.append(f"  - Update checklist: `mcp__switchboard__update_task_checklist(item_id=<id>, done=true)`")
    parts.append(f"  - Update phase: `mcp__switchboard__update_task_phase(task_id='{task['id']}', phase='implementing', detail='...')`")
    parts.append(f"  - Post progress: `mcp__switchboard__post_task_message(task_id='{task['id']}', author='cc-worker', type='progress', content='...')`")
    parts.append(f"  - Post question (will pause session): `mcp__switchboard__post_task_message(task_id='{task['id']}', author='cc-worker', type='question', content='...')`")
    parts.append("- **Update each checklist item as you complete it.** This is how progress is tracked.")
    parts.append("- When done, commit your work, **push your branch** (`git push origin {branch}`), and post a result summary as type='result'.")
    parts.append("- **Always push your branch before finishing.** Your work is headless — unpushed code has no value.")
    parts.append("- Before finishing, post a handoff message with key decisions, gotchas, and notes for the next task:")
    parts.append(f"  `mcp__switchboard__post_task_message(task_id='{task['id']}', author='cc-worker', type='handoff', content='...')`")
    parts.append("")

    parts.append("## SAFETY: Running tests and processes")
    parts.append("- Use `timeout 60 pytest ...` for targeted test runs — always wrap with timeout")
    parts.append("- NEVER use kill, pkill, or killall directly — you WILL terminate yourself")
    parts.append("- If a process hangs, let the timeout handle it or escalate to needs-review")
    parts.append("- Run targeted tests (specific files/functions) during development, the gate handles the full suite")
    parts.append("- If you need to stop a background process, use `timeout` on the original command instead")
    parts.append("")

    # Grounding phase instructions (skip for revision retries — they already know the code)
    if not review_feedback:
        parts.append("## Grounding Phase")
        parts.append("GROUNDING PHASE (do this BEFORE coding):")
        parts.append("1. Read the relevant source files for this task")
        parts.append("2. Review the spec — understand WHY this is being requested, not just WHAT")
        parts.append("3. Review each deliverable in the checklist against the actual code")
        parts.append("4. Adjust deliverables using the checklist tools: fix inaccuracies, add missing items, remove irrelevant ones. Small adjustments are fine to make silently.")
        parts.append("5. If the approach fundamentally won't work, scope is significantly larger than expected, or you see a better way to achieve the goal → set status to needs-review and explain")
        parts.append(f"6. Post your implementation plan as a type='plan' message with file-level detail: `mcp__switchboard__post_task_message(task_id='{task['id']}', author='cc-worker', type='plan', content='...')`")
        parts.append("7. Then begin coding")
        parts.append("")

    if project.get("test_command"):
        parts.append(f"## Testing")
        if task.get("auto_test"):
            parts.append(f"**Tests will be run automatically** after you complete your work (`{project['test_command']}`).")
            parts.append("Do NOT run the full test suite yourself — the gate handles this. If tests fail, you will be retried with the failure output.")
            parts.append("Only run targeted tests if you need to debug a specific piece of logic during development.")
        else:
            parts.append(f"Run tests with: `{project['test_command']}`")
        parts.append("")

    if escalation_criteria:
        parts.append("## Escalation Criteria")
        parts.append(escalation_criteria)
        parts.append("")

    return "\n".join(parts)


def _build_resume_prompt(task: dict) -> str:
    """Build prompt for resuming a paused task."""
    return (
        f"Resume task '{task['id']}'. Check the switchboard for any new answers to your questions "
        f"(mcp__switchboard__read_task_messages(task_id='{task['id']}')), then continue working."
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

async def _setup_log_dir(worktree_path: str) -> Path:
    """Create .switchboard log directory in the worktree.

    Created as the worker user (who owns the worktree), with group-write
    so the service user can also write dispatch/session logs.

    Also ensures .switchboard is gitignored and removes any stale
    git-tracked .switchboard files (which cause permission issues
    when inherited from parent branches).
    """
    log_dir = Path(worktree_path) / ".switchboard"

    # If .switchboard exists and is git-tracked, remove tracked files first
    # (they'll have wrong ownership from git checkout)
    stdout, _, rc = await _run_as_worker(
        "git", "-C", worktree_path, "ls-files", ".switchboard",
    )
    if rc == 0 and stdout.strip():
        log.info(f"Removing git-tracked .switchboard files from {worktree_path}")
        await _run_as_worker("git", "-C", worktree_path, "rm", "-rf", "--cached", ".switchboard")

    # Ensure .switchboard is gitignored so CC never commits it
    gitignore_path = Path(worktree_path) / ".gitignore"
    if gitignore_path.exists():
        content = gitignore_path.read_text()
        if ".switchboard" not in content:
            await _run_as_worker("sh", "-c", f"echo '.switchboard/' >> {gitignore_path}")
    else:
        await _run_as_worker("sh", "-c", f"echo '.switchboard/' > {gitignore_path}")

    # Remove any stale files from a previous task (wrong ownership)
    if log_dir.exists():
        await _run_as_worker("rm", "-rf", str(log_dir))

    await _run_as_worker("mkdir", "-p", str(log_dir))
    await _run_as_worker("chmod", "775", str(log_dir))
    return log_dir


def _open_shared(path, mode="a"):
    """Open a file with group-writable umask (for switchboard-svc + switchboard user)."""
    old = os.umask(0o002)
    try:
        return open(path, mode)
    finally:
        os.umask(old)


def _write_dispatch_log(log_dir: Path, task_id: str, session_id: str,
                        max_turns: int, max_wall_clock: int,
                        worktree_path: str, is_resume: bool,
                        model: str = "sonnet"):
    """Write dispatch metadata to log file."""
    log_path = log_dir / "dispatch.log"
    with _open_shared(log_path) as f:
        f.write(f"[{db.now_iso()}] {'Resuming' if is_resume else 'Dispatching'} task {task_id}\n")
        f.write(f"  session_id: {session_id}\n")
        f.write(f"  model: {model}\n")
        f.write(f"  max_turns: {max_turns}\n")
        f.write(f"  max_wall_clock: {max_wall_clock}m\n")
        f.write(f"  worktree: {worktree_path}\n")


def _tail_file(path: str, n: int = 20) -> str:
    """Read last N lines from a file."""
    try:
        with open(path) as f:
            lines = f.readlines()
            return "".join(lines[-n:])
    except Exception:
        return "(could not read log)"


# ---------------------------------------------------------------------------
# Agent SDK Dispatch
# ---------------------------------------------------------------------------

async def _run_sdk_session(
    task_id: str, prompt: str, worktree_path: str,
    session_id: str | None, is_resume: bool,
    max_turns: int, max_wall_clock_minutes: int,
    log_dir: Path, model: str = "sonnet",
) -> None:
    """Run a CC session via the Agent SDK. Blocks until complete."""
    stderr_path = log_dir / "cc-stderr.log"
    stderr_log = _open_shared(stderr_path)

    # Build SDK options — run CC as restricted 'switchboard' user
    worker_home = pwd.getpwnam(WORKER_USER).pw_dir

    # Merge user-level MCP servers from ~/.claude.json (e.g. shopify-ai)
    mcp_servers = {
        "switchboard": {
            "type": "http",
            "url": f"http://localhost:{os.environ.get('SWITCHBOARD_PORT', '8100')}/mcp",
        },
        "graphiti": {
            "type": "http",
            "url": "http://localhost:8002/mcp",
        },
    }
    try:
        with open(os.path.join(worker_home, ".claude.json")) as f:
            for name, cfg in json.load(f).get("mcpServers", {}).items():
                if name not in mcp_servers:
                    mcp_servers[name] = cfg
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        pass

    options = ClaudeAgentOptions(
        user=WORKER_USER,
        cwd=str(worktree_path),
        env={"HOME": worker_home},
        permission_mode="bypassPermissions",
        model=model,
        max_turns=max_turns,
        setting_sources=["user", "project"],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": prompt if not is_resume else "",
        },
        mcp_servers=mcp_servers,
        debug_stderr=stderr_log,
        extra_args={"replay-user-messages": None},
    )

    # If resuming, use the resume option
    if is_resume and session_id:
        options.resume = session_id

    try:
        result_msg = None
        timeout_seconds = max_wall_clock_minutes * 60
        session_log_path = log_dir / "session.jsonl"

        def _log_message(msg):
            """Write a message to the session JSONL log."""
            entry = {"timestamp": db.now_iso(), "type": type(msg).__name__}
            try:
                if isinstance(msg, SystemMessage):
                    entry["subtype"] = getattr(msg, "subtype", None)
                elif isinstance(msg, AssistantMessage):
                    entry["content"] = []
                    for block in (msg.content or []):
                        if isinstance(block, TextBlock):
                            entry["content"].append({"type": "text", "text": block.text})
                        elif isinstance(block, ToolUseBlock):
                            entry["content"].append({
                                "type": "tool_use", "name": block.name,
                                "input": str(block.input)[:5000],
                            })
                    entry["stop_reason"] = getattr(msg, "stop_reason", None)
                    entry["model"] = getattr(msg, "model", None)
                elif isinstance(msg, UserMessage):
                    entry["content"] = []
                    content = msg.content
                    if isinstance(content, str):
                        entry["content"].append({"type": "text", "text": content})
                    else:
                        for block in (content or []):
                            if isinstance(block, ToolResultBlock):
                                entry["content"].append({
                                    "type": "tool_result",
                                    "tool_use_id": block.tool_use_id,
                                    "preview": str(block.content or "")[:5000],
                                    "is_error": getattr(block, "is_error", None),
                                })
                elif isinstance(msg, ResultMessage):
                    entry["subtype"] = getattr(msg, "subtype", None)
                    entry["result"] = msg.result or ""
                    entry["num_turns"] = msg.num_turns
                    entry["session_id"] = getattr(msg, "session_id", None)
                    entry["cost_usd"] = msg.total_cost_usd
                    entry["duration_ms"] = getattr(msg, "duration_ms", None)
                    entry["is_error"] = getattr(msg, "is_error", None)
                with _open_shared(session_log_path) as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception as e:
                log.warning(f"Failed to log message: {e}")

        async def _check_for_injections(client: ClaudeSDKClient, seen_ids: set[int]) -> None:
            """Poll DB for new messages and inject them via client.query()."""
            try:
                thread = await db.read_task_messages(task_id)
                for msg in thread.get("messages", []):
                    msg_id = msg.get("id")
                    if not msg_id or msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)
                    # Only inject human-authored messages, not dispatcher/cc-worker status
                    if msg.get("author") in ("dispatcher", "cc-worker"):
                        continue
                    author = msg.get("author", "user")
                    msg_type = msg.get("type", "note")
                    title = msg.get("title") or ""
                    content = msg.get("content", "")
                    injection = (
                        f"--- LIVE MESSAGE FROM {author.upper()} ({msg_type}) ---\n"
                        f"{(title + chr(10)) if title else ''}"
                        f"{content}\n"
                        f"--- END LIVE MESSAGE ---\n\n"
                        f"The above message was just posted to your task thread. "
                        f"Read it carefully and adjust your work accordingly."
                    )
                    log.info(f"Injecting message {msg_id} into task {task_id}")
                    await client.query(injection)
                    await notify.task_heartbeat(
                        task_id=task_id, turns=0,
                        elapsed_s=0, last_tool=f"[injected msg from {author}]",
                    )
            except Exception as e:
                log.warning(f"Message poll error for {task_id}: {e}")

        async def _poll_and_inject(client: ClaudeSDKClient, seen_ids: set[int], done: asyncio.Event):
            """Background task: poll DB for new messages, inject via client.query()."""
            while not done.is_set():
                try:
                    await asyncio.wait_for(done.wait(), timeout=MESSAGE_POLL_INTERVAL)
                    break  # done was set
                except asyncio.TimeoutError:
                    pass  # poll interval elapsed
                await _check_for_injections(client, seen_ids)

        async def _run():
            nonlocal result_msg
            start_time = time.monotonic()
            last_heartbeat = start_time
            heartbeat_interval = 90  # seconds
            turn_count = 0
            running_cost = 0.0
            last_tool_name = None

            actual_prompt = _build_resume_prompt({"id": task_id}) if is_resume else prompt

            # Snapshot existing message IDs so we only inject NEW ones
            seen_ids: set[int] = set()
            thread = await db.read_task_messages(task_id)
            for msg in thread.get("messages", []):
                if msg.get("id"):
                    seen_ids.add(msg["id"])

            done_event = asyncio.Event()

            async with ClaudeSDKClient(options=options) as client:
                _active_clients[task_id] = client

                # Start background message injection poller
                poll_task = asyncio.create_task(
                    _poll_and_inject(client, seen_ids, done_event),
                    name=f"msg-poll-{task_id}",
                )

                try:
                    # Send initial prompt
                    await client.query(actual_prompt)

                    # Process all messages until ResultMessage
                    async for message in client.receive_response():
                        _log_message(message)

                        if isinstance(message, AssistantMessage):
                            turn_count += 1
                            for block in (message.content or []):
                                if isinstance(block, ToolUseBlock):
                                    last_tool_name = block.name

                        if isinstance(message, ResultMessage):
                            result_msg = message
                            if message.session_id:
                                await db.update_task(task_id, session_id=message.session_id)
                            running_cost = message.total_cost_usd or 0

                        # Heartbeat
                        now = time.monotonic()
                        if now - last_heartbeat >= heartbeat_interval:
                            last_heartbeat = now
                            await notify.task_heartbeat(
                                task_id=task_id,
                                turns=turn_count,
                                elapsed_s=now - start_time,
                                last_tool=last_tool_name,
                            )

                        # Update last_activity on each message
                        await db.update_task(task_id, last_activity=db.now_iso())

                finally:
                    done_event.set()
                    poll_task.cancel()
                    try:
                        await poll_task
                    except asyncio.CancelledError:
                        pass
                    _active_clients.pop(task_id, None)

        try:
            await asyncio.wait_for(_run(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            log.warning(f"Task {task_id}: wall clock timeout ({max_wall_clock_minutes}m)")
            await db.update_task(task_id, status="needs-review")
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title="Wall clock timeout",
                content=f"Task hit the {max_wall_clock_minutes} minute wall clock limit. "
                        "Work is preserved in the worktree. Resume or adjust limits.",
            )
            await notify.task_needs_review(
                task_id=task_id,
                reason=f"Wall clock timeout ({max_wall_clock_minutes}m). Work preserved in worktree.",
            )
            with _open_shared(log_dir / "dispatch.log") as f:
                f.write(f"[{db.now_iso()}] Wall clock timeout ({max_wall_clock_minutes}m)\n")
            return

        # Process result
        if result_msg:
            _log_result(log_dir, result_msg)
            await _update_usage(task_id, result_msg)

            if result_msg.stop_reason == "max_turns" or (
                result_msg.num_turns and result_msg.num_turns >= max_turns
            ):
                await db.update_task(task_id, status="turns-exhausted")
                await db.post_task_message(
                    task_id=task_id, author="dispatcher", type="status",
                    title="Turns exhausted",
                    content=f"CC session hit the {max_turns}-turn limit.\n\n"
                            f"Turns: {result_msg.num_turns} | "
                            f"Cost: ${result_msg.total_cost_usd or 0:.4f}\n\n"
                            f"Work is preserved in the worktree. Resume to continue with the same session.",
                )

                # Still push and try the gate — CC may have finished the work
                task = await db.get_task(task_id)
                await _ensure_branch_pushed(task_id, task)
                if not task.get("gate_passed_at"):
                    project = await db.get_project(task["project_id"])
                    if task.get("auto_test") and project and project.get("test_command"):
                        await _run_test_gate(task_id, project, task)
                    elif task.get("auto_review"):
                        await _dispatch_review(task_id, project, task)

                # Only notify for manual review if gate didn't auto-handle it
                task = await db.get_task(task_id)
                if not task.get("gate_passed_at") and task.get("gate_status") not in ("testing", "reviewing", "test-passed"):
                    await notify.task_needs_review(
                        task_id=task_id,
                        reason=f"Turns exhausted ({result_msg.num_turns}/{max_turns}). Resume to continue.",
                    )
            elif result_msg.is_error and result_msg.result and "hit your limit" in result_msg.result.lower():
                # Rate limited — compute retry_after and auto-retry when limits reset
                reset_match = re.search(r'resets?\s+(\d{1,2})(am|pm)?\s*\(?(\w+)?\)?', result_msg.result, re.IGNORECASE)
                retry_after_iso = None
                reset_info = ""
                if reset_match:
                    hour = int(reset_match.group(1))
                    ampm = (reset_match.group(2) or "").lower()
                    tz_hint = (reset_match.group(3) or "UTC").upper()
                    if ampm == "pm" and hour < 12:
                        hour += 12
                    elif ampm == "am" and hour == 12:
                        hour = 0
                    # Compute next occurrence of that hour in UTC
                    now = datetime.now(timezone.utc)
                    retry_at = now.replace(hour=hour, minute=5, second=0, microsecond=0)
                    if retry_at <= now:
                        retry_at += timedelta(days=1)
                    retry_after_iso = retry_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                    reset_info = f" Will auto-retry at {retry_at.strftime('%H:%M UTC')}."

                await db.update_task(task_id, status="rate-limited",
                                     retry_after=retry_after_iso)
                await db.post_task_message(
                    task_id=task_id, author="dispatcher", type="status",
                    title="Rate limited",
                    content=f"CC hit usage limits.{reset_info}\n\n"
                            f"Turns: {result_msg.num_turns} | "
                            f"Cost: ${result_msg.total_cost_usd or 0:.4f}\n\n"
                            f"Work is preserved.{' Auto-retry scheduled.' if retry_after_iso else ' Retry manually after limits reset.'}",
                )
                log.warning(f"Task {task_id}: rate limited, retry_after={retry_after_iso}")
                await _drain_queue()
            elif result_msg.is_error:
                await db.update_task(task_id, status="failed")
                await db.post_task_message(
                    task_id=task_id, author="dispatcher", type="status",
                    title="Task failed",
                    content=f"CC session ended with error.\n\nStop reason: {result_msg.stop_reason}\n"
                            f"Turns: {result_msg.num_turns}\n\n"
                            f"Result: {result_msg.result or '(no result)'}",
                )
                await notify.task_failed(
                    task_id=task_id,
                    error=result_msg.result or result_msg.stop_reason or "Unknown error",
                    turns=result_msg.num_turns,
                    cost_usd=result_msg.total_cost_usd or 0,
                )
                # Slot freed — drain FIFO queue
                await _drain_queue()
            else:
                await db.update_task(task_id, status="completed")
                await db.post_task_message(
                    task_id=task_id, author="dispatcher", type="status",
                    title="Task completed",
                    content=f"CC session completed successfully.\n\n"
                            f"Turns: {result_msg.num_turns} | "
                            f"Duration: {result_msg.duration_ms / 1000:.0f}s | "
                            f"Cost: ${result_msg.total_cost_usd or 0:.4f}\n\n"
                            f"Result: {result_msg.result or '(no result)'}",
                )
                checklist = await db.get_checklist(task_id)
                done = sum(1 for c in checklist if c.get("done"))
                await notify.task_completed(
                    task_id=task_id,
                    turns=result_msg.num_turns,
                    duration_s=(result_msg.duration_ms or 0) / 1000,
                    cost_usd=result_msg.total_cost_usd or 0,
                    checklist_done=done,
                    checklist_total=len(checklist),
                    result_preview=result_msg.result,
                )

                # Auto-push branch before gate pipeline
                task = await db.get_task(task_id)
                await _ensure_branch_pushed(task_id, task)

                # Check if this is a review task — process result on parent
                if task.get("parent_task_id"):
                    await _process_review_result(task_id, task["parent_task_id"])
                elif task.get("gate_passed_at"):
                    # Gate already passed previously — this is a manual resume (e.g. fixing merge conflicts)
                    # Re-trigger post-gate pipeline so auto-merge / chain advancement runs again
                    log.info(f"Task {task_id}: gate already passed, re-triggering post-gate pipeline (manual resume)")
                    await _check_and_dispatch_dependents(task_id)
                else:
                    # First-pass completion — run the gate pipeline
                    project = await db.get_project(task["project_id"])
                    if task.get("auto_test") and project and project.get("test_command"):
                        await _run_test_gate(task_id, project, task)
                    elif task.get("auto_review"):
                        await _dispatch_review(task_id, project, task)
                    else:
                        await db.update_task(task_id, gate_status="passed", gate_passed_at=db.now_iso())
                        await _check_and_dispatch_dependents(task_id)
        else:
            # No result message — shouldn't happen but handle gracefully
            await db.update_task(task_id, status="needs-review")
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title="Session ended without result",
                content="CC session ended but no ResultMessage was received. Check logs.",
            )
            await notify.task_needs_review(
                task_id=task_id, reason="Session ended without a ResultMessage. Check logs.",
            )

    except Exception as e:
        error_str = str(e)
        is_sigterm = any(s in error_str for s in ("exit code -15", "exit code -9", "exit code 143", "exit code 137"))

        if is_sigterm:
            # SIGTERM/SIGKILL — external kill (service restart, OOM), not a real failure.
            # Keep as working so startup recovery auto-resumes.
            log.warning(f"SDK session killed by signal for task {task_id}: {e}")
            await db.update_task(task_id, recovery_priority=True)
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title="Session killed by signal",
                content=f"CC process was killed externally (likely service restart).\n"
                        f"Task will auto-resume on next startup.\n\n```\n{error_str}\n```",
            )
        else:
            log.exception(f"SDK session error for task {task_id}: {e}")
            await db.update_task(task_id, status="failed")
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title="Dispatch error",
                content=f"SDK session raised an exception:\n\n```\n{e}\n```",
            )
        # If this was a review task, still try to process any review it posted
        task = await db.get_task(task_id)
        if task and task.get("parent_task_id"):
            try:
                await _process_review_result(task_id, task["parent_task_id"])
            except Exception:
                log.exception(f"Failed to process review result for crashed review task {task_id}")
        await notify.task_failed(task_id=task_id, error=str(e))
        with _open_shared(log_dir / "dispatch.log") as f:
            f.write(f"[{db.now_iso()}] SDK error: {e}\n")
        # Slot freed — drain FIFO queue
        await _drain_queue()
    finally:
        stderr_log.close()


def _log_result(log_dir: Path, result: ResultMessage):
    """Write result metadata to dispatch log."""
    with _open_shared(log_dir / "dispatch.log") as f:
        f.write(f"[{db.now_iso()}] Session complete\n")
        f.write(f"  session_id: {result.session_id}\n")
        f.write(f"  turns: {result.num_turns}\n")
        f.write(f"  duration_ms: {result.duration_ms}\n")
        f.write(f"  duration_api_ms: {result.duration_api_ms}\n")
        f.write(f"  is_error: {result.is_error}\n")
        f.write(f"  stop_reason: {result.stop_reason}\n")
        f.write(f"  cost_usd: {result.total_cost_usd}\n")
        if result.usage:
            f.write(f"  usage: {json.dumps(result.usage)}\n")


async def _run_subtask(
    task_id: str,
    subtask_type: str,
    prompt: str,
    model: str = "opus",
    max_turns: int = 30,
) -> dict:
    """Run a lightweight CC session in the parent's worktree.

    No separate worktree, no setup_command, no gate pipeline.
    Returns the subtask record.
    """
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Parent task '{task_id}' not found")

    worktree = task.get("worktree_path")
    if not worktree or not os.path.exists(worktree):
        raise ValueError(f"No worktree for task '{task_id}'")

    # Generate subtask ID
    existing = await db.get_subtasks(task_id)
    count = sum(1 for s in existing if s["type"] == subtask_type) + 1
    subtask_id = f"{task_id}/{subtask_type}-{count}"

    await db.create_subtask(
        id=subtask_id, task_id=task_id, type=subtask_type,
        prompt=prompt, model=model,
    )

    # Build SDK options — same as _run_sdk_session but simpler
    worker_home = pwd.getpwnam(WORKER_USER).pw_dir
    mcp_servers = {
        "switchboard": {"type": "http", "url": f"http://localhost:{os.environ.get('SWITCHBOARD_PORT', '8100')}/mcp"},
        "graphiti": {"type": "http", "url": "http://localhost:8002/mcp"},
    }
    try:
        with open(os.path.join(worker_home, ".claude.json")) as f:
            for name, cfg in json.load(f).get("mcpServers", {}).items():
                if name not in mcp_servers:
                    mcp_servers[name] = cfg
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        pass

    log_dir = Path(worktree) / ".switchboard"
    log_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = log_dir / f"{subtask_type}-{count}-stderr.log"
    stderr_log = _open_shared(stderr_path)

    options = ClaudeAgentOptions(
        user=WORKER_USER,
        cwd=str(worktree),
        env={"HOME": worker_home},
        permission_mode="bypassPermissions",
        model=model,
        max_turns=max_turns,
        setting_sources=["user", "project"],
        system_prompt={"type": "preset", "preset": "claude_code", "append": prompt},
        mcp_servers=mcp_servers,
        debug_stderr=stderr_log,
        extra_args={"replay-user-messages": None},
    )

    result_msg = None
    log.info(f"Running subtask {subtask_id} (type={subtask_type}, model={model})")

    # Subtask session log — write to .switchboard/{type}-{count}-session.jsonl
    subtask_log_path = log_dir / f"{subtask_type}-{count}-session.jsonl"
    subtask_log_file = _open_shared(subtask_log_path)

    def _log_subtask_msg(msg):
        entry = {"timestamp": db.now_iso(), "type": type(msg).__name__}
        try:
            if isinstance(msg, AssistantMessage):
                entry["content"] = []
                for block in (msg.content or []):
                    if isinstance(block, TextBlock):
                        entry["content"].append({"type": "text", "text": block.text})
                    elif isinstance(block, ToolUseBlock):
                        entry["content"].append({"type": "tool_use", "name": block.name, "input": str(block.input)[:5000]})
            elif isinstance(msg, UserMessage):
                content = msg.content
                if isinstance(content, str):
                    entry["content"] = [{"type": "text", "text": content}]
                else:
                    entry["content"] = [{"type": "tool_result"} for _ in (content or [])]
            subtask_log_file.write(json.dumps(entry) + "\n")
            subtask_log_file.flush()
        except Exception:
            pass

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                _log_subtask_msg(message)
                if isinstance(message, ResultMessage):
                    result_msg = message
    except Exception as e:
        log.exception(f"Subtask {subtask_id} error: {e}")
        await db.update_subtask(subtask_id, status="failed",
                                result=str(e), completed_at=db.now_iso())
        return await db.get_subtask(subtask_id)
    finally:
        stderr_log.close()
        subtask_log_file.close()

    if result_msg:
        input_tokens = 0
        output_tokens = 0
        if result_msg.usage:
            input_tokens = (result_msg.usage.get("input_tokens", 0)
                            + result_msg.usage.get("cache_creation_input_tokens", 0)
                            + result_msg.usage.get("cache_read_input_tokens", 0))
            output_tokens = result_msg.usage.get("output_tokens", 0)

        await db.update_subtask(
            subtask_id,
            status="completed" if not result_msg.is_error else "failed",
            result=result_msg.result or "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=result_msg.total_cost_usd or 0.0,
            duration_ms=result_msg.duration_ms or 0,
            completed_at=db.now_iso(),
        )

        # Roll up cost to parent task
        await _update_usage(task_id, result_msg)
        log.info(f"Subtask {subtask_id} completed (cost=${result_msg.total_cost_usd or 0:.4f})")
    else:
        await db.update_subtask(subtask_id, status="failed",
                                result="No result received", completed_at=db.now_iso())
        log.warning(f"Subtask {subtask_id} ended without ResultMessage")

    return await db.get_subtask(subtask_id)


async def _run_test_gate(task_id: str, project: dict, task: dict) -> None:
    """Run the project's test_command after task completion. Auto-retry on failure."""
    test_command = project.get("test_command")
    if not test_command:
        log.warning(f"Task {task_id}: auto_test enabled but project has no test_command")
        await db.update_task(task_id, gate_status="passed", gate_passed_at=db.now_iso())
        return

    await db.update_task(task_id, gate_status="testing")
    worktree = task.get("worktree_path")
    if not worktree or not os.path.exists(worktree):
        log.error(f"Task {task_id}: no worktree for test gate")
        await db.update_task(task_id, gate_status="test-failed")
        return

    log.info(f"Task {task_id}: running test gate: {test_command}")
    stdout, stderr, rc = await _run_as_worker(
        "sh", "-c", f"cd {shlex.quote(worktree)} && {test_command}",
    )
    test_output = stdout.decode(errors="replace") + stderr.decode(errors="replace")

    # Store structured test output on the task
    task = await db.get_task(task_id)
    stdout_lines = test_output.split("\n")
    stdout_tail = "\n".join(stdout_lines[-100:])
    last_test_output = json.dumps({
        "exit_code": rc,
        "stdout_tail": stdout_tail,
        "ran_at": db.now_iso(),
        "attempt": task.get("current_attempt") or 1,
    })
    await db.update_task(task_id, last_test_output=last_test_output)

    if rc == 0:
        # Tests passed — but don't set gate_passed_at if review still pending
        task = await db.get_task(task_id)
        if task.get("auto_review"):
            await db.update_task(task_id, gate_status="test-passed")
        else:
            await db.update_task(task_id, gate_status="passed", gate_passed_at=db.now_iso())
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="test-result",
            title="Tests passed",
            content=f"```\n{_tail_lines(test_output, 3000)}\n```",
        )
        log.info(f"Task {task_id}: test gate passed")

        # If auto_review is enabled, dispatch a review instead of passing immediately
        if task.get("auto_review"):
            await _dispatch_review(task_id, project, task)
        else:
            await notify.task_needs_review(
                task_id=task_id, reason="Gate passed: tests passed.",
            )
            await _check_and_dispatch_dependents(task_id)
    else:
        # Refresh task to get current retry count
        task = await db.get_task(task_id)
        retries = (task.get("gate_retries") or 0) + 1
        max_retries = task.get("max_gate_retries") or 3
        await db.update_task(task_id, gate_status="test-failed", gate_retries=retries)
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="test-result",
            title=f"Tests failed (attempt {retries}/{max_retries})",
            content=f"```\n{_tail_lines(test_output, 3000)}\n```",
        )
        log.warning(f"Task {task_id}: test gate failed (attempt {retries}/{max_retries})")

        if retries < max_retries:
            # Auto-retry: dispatch new session with test failure as review feedback
            log.info(f"Task {task_id}: auto-retrying after test failure")
            await retry_task(task_id)
        else:
            await db.update_task(task_id, status="needs-review")
            await notify.task_needs_review(
                task_id=task_id,
                reason=f"Tests failed {retries} times. Manual intervention needed.",
            )


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


_DEFAULT_REVIEW_IGNORE_PATTERNS = [
    ".switchboard/",
    ".lock",
    "package-lock.json",
    "composer.lock",
    ".gitignore",
]

_TAG_REVIEW_GUIDANCE = {
    "backend": (
        "Focus on: error handling and edge cases, test coverage for failure paths, "
        "security (input validation, SQL injection, auth checks), API contract correctness."
    ),
    "frontend": (
        "Focus on: UX and user-facing correctness, accessibility (ARIA, keyboard nav), "
        "responsive behavior across screen sizes, render performance."
    ),
    "testing": (
        "Focus on: test quality and assertion correctness (assertions match spec, not just code output), "
        "coverage of edge cases and failure modes, test isolation and fixture design."
    ),
}

_DEFAULT_REVIEW_GUIDANCE = (
    "Balanced review: correctness vs spec, test quality, edge cases, code clarity."
)


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


async def _dispatch_review(task_id: str, project: dict, task: dict) -> None:
    """Run a lightweight review subtask in the parent's worktree."""
    await db.update_task(task_id, gate_status="reviewing")

    diff_output = await _get_branch_diff(task)

    # --- Component context ---
    component = None
    if task.get("component_id"):
        component = await db.get_component(task["component_id"])

    component_section = "No component assigned."
    if component:
        component_section = (
            f"**Name:** {component['name']}\n"
            f"**Description:** {component.get('description') or '(none)'}\n"
            f"**Phase:** {component.get('phase') or 'unknown'}"
        )

    # --- Ignore patterns ---
    ignore_patterns = _DEFAULT_REVIEW_IGNORE_PATTERNS[:]
    # Component-level patterns override/extend defaults
    if component and component.get("review_ignore_patterns"):
        raw = component["review_ignore_patterns"]
        if isinstance(raw, str):
            import json as _json
            raw = _json.loads(raw)
        if isinstance(raw, list):
            ignore_patterns = raw
    elif project.get("review_ignore_patterns"):
        raw = project["review_ignore_patterns"]
        if isinstance(raw, str):
            import json as _json
            raw = _json.loads(raw)
        if isinstance(raw, list):
            ignore_patterns = raw

    filtered_diff = _filter_diff_by_ignore_patterns(diff_output, ignore_patterns)
    ignore_section = "\n".join(f"- `{p}`" for p in ignore_patterns)

    # --- Punchlist claims ---
    punchlist_section = "None."
    if component:
        claimed_items = await db.list_punchlist(
            component["id"], include_done=False, claimed_by=task_id
        )
        if claimed_items:
            lines = [
                "This task claims to fix the following punchlist items. "
                "Verify they are actually addressed:"
            ]
            for it in claimed_items:
                lines.append(f"- #{it['id']}: {it['item']}")
            punchlist_section = "\n".join(lines)

    # --- Tag-based review focus ---
    tags = await db.get_task_tags(task_id)
    review_focus = _DEFAULT_REVIEW_GUIDANCE
    for tag in tags:
        if tag in _TAG_REVIEW_GUIDANCE:
            review_focus = _TAG_REVIEW_GUIDANCE[tag]
            break  # first matching tag wins

    # --- Spec content ---
    pinned = await db.get_task_pinned(task_id)
    spec_content = pinned["content"] if pinned else "(no spec)"

    # --- Thread context (course corrections) ---
    thread = await db.read_task_messages(task_id)
    thread_msgs = thread.get("messages", [])
    human_msgs = [m for m in thread_msgs if m.get("author") not in ("dispatcher", "cc-worker")]
    thread_context = ""
    if human_msgs:
        thread_lines = []
        for m in human_msgs:
            author = m.get("author", "user")
            title = m.get("title", "")
            content = m.get("content", "")
            thread_lines.append(f"**[{author}]** {(title + ': ') if title else ''}{content}")
        thread_context = f"""

## Course Corrections / Notes from User
The following messages were posted during development. These override or refine
the original spec — treat them as authoritative when they conflict with the spec.

{chr(10).join(thread_lines)}
"""

    review_prompt = f"""# Code Review: {task['goal']}

## Component Context
{component_section}

## Ignore These Files
The following patterns were excluded from the diff below:
{ignore_section}

## Punchlist Items Claimed
{punchlist_section}

## Original Spec
{spec_content}
{thread_context}
## Changes to Review
```
{filtered_diff[:10000]}
```

## Review Focus
{review_focus}

## Review Criteria
- Do changes match the spec AND any course corrections above? Every requirement addressed?
- Are tests testing the RIGHT things? (assertions match spec, not code output)
- Any obvious bugs, edge cases, or security issues?
- Code quality: naming, structure, unnecessary complexity?

Post your review on the task:
mcp__switchboard__post_task_message(task_id='{task_id}', author='cc-worker', type='review', ...)

If clean: title="APPROVED"
If changes needed: title="CHANGES REQUESTED" and list specific issues
"""

    log.info(f"Running subtask review for {task_id}")
    try:
        subtask = await _run_subtask(
            task_id=task_id,
            subtask_type="review",
            prompt=review_prompt,
            model=task.get("review_model") or "opus",
        )

        if subtask.get("status") == "completed":
            await _process_review_result_inline(task_id)
        else:
            log.warning(f"Review subtask failed for {task_id}: {subtask.get('error', 'unknown')}")
            task = await db.get_task(task_id)
            retries = (task.get("gate_retries") or 0) + 1
            max_retries = task.get("max_gate_retries") or 3
            await db.update_task(task_id, gate_status="review-failed", gate_retries=retries)
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title="Review failed",
                content=f"Review subtask did not complete (attempt {retries}/{max_retries}).\n\n"
                        f"Error: {subtask.get('error', 'process killed or crashed')}",
            )
            if retries < max_retries:
                log.info(f"Retrying review for {task_id} (attempt {retries + 1})")
                await _dispatch_review(task_id, await db.get_project(task["project_id"]), task)
            else:
                await db.update_task(task_id, status="needs-review")
                await notify.task_needs_review(task_id, reason="Review failed after max retries.")
    except Exception as e:
        log.error(f"Failed to run review subtask for {task_id}: {e}")
        task = await db.get_task(task_id)
        retries = (task.get("gate_retries") or 0) + 1
        max_retries = task.get("max_gate_retries") or 3
        await db.update_task(task_id, gate_status="review-failed", gate_retries=retries)
        if retries < max_retries:
            try:
                await _dispatch_review(task_id, await db.get_project(task["project_id"]), task)
            except Exception:
                log.exception(f"Review retry also failed for {task_id}")
                await db.update_task(task_id, status="needs-review")
        else:
            await db.update_task(task_id, status="needs-review")
            await notify.task_needs_review(task_id, reason=f"Review failed: {e}")


async def _process_review_result_inline(task_id: str) -> None:
    """Check review messages on task and process approval/rejection."""
    msgs = await db.read_task_messages(task_id)
    review_msg = next(
        (m for m in reversed(msgs.get("messages", []))
         if m.get("type") == "review"),
        None,
    )

    if review_msg and "APPROVED" in (review_msg.get("title") or "").upper():
        log.info(f"Review approved for {task_id}")
        await db.update_task(task_id, gate_status="passed", gate_passed_at=db.now_iso())
        await _check_and_dispatch_dependents(task_id)
    else:
        task = await db.get_task(task_id)
        retries = (task.get("gate_retries") or 0) + 1
        max_retries = task.get("max_gate_retries") or 3
        await db.update_task(task_id, gate_status="review-failed", gate_retries=retries)
        log.warning(f"Review failed for {task_id} (attempt {retries}/{max_retries})")

        if retries < max_retries:
            await retry_task(task_id)
        else:
            await db.update_task(task_id, status="needs-review")
            await notify.task_needs_review(task_id, reason="Review failed after max retries.")


async def _process_review_result(review_task_id: str, parent_task_id: str) -> None:
    """Check if review approved or requested changes."""
    msgs = await db.read_task_messages(parent_task_id)
    review_msg = next(
        (m for m in reversed(msgs.get("messages", []))
         if m.get("type") == "review"),
        None,
    )

    if review_msg and "APPROVED" in (review_msg.get("title") or "").upper():
        log.info(f"Review approved for {parent_task_id}")
        await db.update_task(parent_task_id, gate_status="passed", gate_passed_at=db.now_iso())
        await _check_and_dispatch_dependents(parent_task_id)
    else:
        parent = await db.get_task(parent_task_id)
        retries = (parent.get("gate_retries") or 0) + 1
        max_retries = parent.get("max_gate_retries") or 3
        await db.update_task(parent_task_id, gate_status="review-failed", gate_retries=retries)
        log.warning(f"Review failed for {parent_task_id} (attempt {retries}/{max_retries})")

        if retries < max_retries:
            await retry_task(parent_task_id)
        else:
            await db.update_task(parent_task_id, status="needs-review")
            await notify.task_needs_review(
                parent_task_id,
                reason="Review failed after max retries.",
            )


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

    # Auto-release worktree after gate pass + merge/PR
    # (do this after merge but before chain dispatch so the worktree is freed)
    await _auto_release_worktree(task_id)

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

    # Fetch latest
    await _run_as_worker("git", "-C", worktree, "fetch", "origin")

    # Attempt rebase
    _, stderr, rc = await _run_as_worker(
        "git", "-C", worktree, "rebase", f"origin/{parent_branch}",
    )

    if rc != 0:
        # Rebase failed — abort and let CC handle it
        await _run_as_worker("git", "-C", worktree, "rebase", "--abort")
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


async def _update_usage(task_id: str, result: ResultMessage):
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
# FIFO Queue Drain
# ---------------------------------------------------------------------------

async def _drain_queue() -> None:
    """Dispatch the oldest eligible queued task if a concurrency slot is available."""
    active = await db.count_active_tasks()
    if active >= db.DEFAULT_MAX_CONCURRENT:
        return

    queued = await db.get_queued_tasks()
    if not queued:
        return

    task = queued[0]  # FIFO — oldest first
    log.info(f"Queue drain: dispatching {task['id']} (queued_at={task['queued_at']})")
    try:
        await dispatch_task(
            project_id=task["project_id"],
            task_id=task["id"],
            goal=task["goal"],
            auto_test=task.get("auto_test", True),
        )
    except Exception as e:
        log.error(f"Queue drain failed for {task['id']}: {e}")


# ---------------------------------------------------------------------------
# Branch Resolution + Auto-Merge
# ---------------------------------------------------------------------------

async def resolve_branch_target(task: dict) -> str:
    """Resolve the merge target branch using config inheritance.

    Priority: depends_on parent branch → task.base_branch → component.base_branch
              → project.default_branch
    """
    # 1. Parent branch (chain branching)
    #    If parent has already merged (worktree released), fall through to
    #    project default — the parent branch no longer exists as a checkout.
    if task.get("depends_on"):
        parent = await db.get_task(task["depends_on"])
        if parent and parent.get("branch"):
            parent_merged = (
                parent.get("status") in ("completed", "merged")
                and parent.get("gate_status") == "passed"
                and not parent.get("worktree_path")
            )
            if not parent_merged:
                return parent["branch"]

    # 2. Task-level override
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


async def _perform_auto_merge(task_id: str) -> bool:
    """Merge task branch into branch_target and push. Returns True on success."""
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

    project = await db.get_project(task["project_id"])
    bare_path = os.path.join(project["working_dir"], ".bare") if project else None

    # Fetch latest
    await _run_as_worker("git", "-C", worktree, "fetch", "origin")

    # Checkout the target branch in the worktree
    _, stderr, rc = await _run_as_worker(
        "git", "-C", worktree, "checkout", branch_target,
    )
    if rc != 0:
        # Branch might be locked by parent's worktree — try releasing it
        if task.get("depends_on"):
            parent_task = await db.get_task(task["depends_on"])
            if (parent_task
                and parent_task.get("branch") == branch_target
                and parent_task.get("worktree_path")
                and parent_task.get("gate_passed_at")):
                log.info(f"Auto-merge {task_id}: releasing parent worktree "
                         f"{parent_task['id']} (branch {branch_target} is locked)")
                try:
                    await release_worktree(parent_task["id"], reason="auto-merge-needs-branch")
                    # Retry checkout after release
                    _, stderr, rc = await _run_as_worker(
                        "git", "-C", worktree, "checkout", branch_target,
                    )
                except Exception as e:
                    log.warning(f"Auto-merge {task_id}: failed to release parent worktree: {e}")

    if rc != 0:
        # Target branch may not exist locally — try creating from origin
        _, stderr, rc = await _run_as_worker(
            "git", "-C", worktree, "checkout", "-b", branch_target, f"origin/{branch_target}",
        )
        if rc != 0:
            log.error(f"Auto-merge {task_id}: cannot checkout {branch_target}: {stderr.decode()}")
            await db.update_task(task_id, status="needs-review",
                                 pr_status="error", pr_error=f"Cannot checkout {branch_target}")
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title="Auto-merge failed",
                content=f"Cannot checkout target branch `{branch_target}`:\n```\n{stderr.decode()[:1000]}\n```",
            )
            return False

    # Pull latest on target
    await _run_as_worker("git", "-C", worktree, "pull", "--ff-only", "origin", branch_target)

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

        # Switch back to task branch
        await _run_as_worker("git", "-C", worktree, "checkout", task_branch)

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

    # Push the merged target branch
    _, stderr, rc = await _run_as_worker(
        "git", "-C", worktree, "push", "origin", branch_target,
    )
    if rc != 0:
        log.error(f"Auto-merge {task_id}: push failed: {stderr.decode()}")
        await db.update_task(task_id, status="needs-review",
                             pr_status="push-failed", pr_error=stderr.decode()[:500])
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Auto-merge push failed",
            content=f"Merge succeeded but push to `{branch_target}` failed:\n```\n{stderr.decode()[:1000]}\n```",
        )
        # Switch back to task branch
        await _run_as_worker("git", "-C", worktree, "checkout", task_branch)
        return False

    # Switch back to task branch for worktree consistency
    await _run_as_worker("git", "-C", worktree, "checkout", task_branch)

    await db.update_task(task_id, status="merged", pushed_at=db.now_iso(), pr_status="merged")
    await db.post_task_message(
        task_id=task_id, author="dispatcher", type="status",
        title="Auto-merged",
        content=f"Branch `{task_branch}` merged into `{branch_target}` and pushed.",
    )
    log.info(f"Auto-merge {task_id}: {task_branch} → {branch_target} success")
    return True


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
        dest.mkdir(parents=True, exist_ok=True)
        for src_file in src.iterdir():
            if src_file.is_file():
                shutil.copy2(src_file, dest / src_file.name)

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
        (dest / "metadata.json").write_text(json.dumps(metadata, indent=2))
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
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", bare_path, "worktree", "remove", "--force", worktree,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.warning(f"Worktree remove failed for {task_id}: {stderr.decode()}")
            else:
                log.info(f"Released worktree for {task_id}: {worktree}")

            # Clean up local branch ref so it doesn't block checkout from other worktrees
            branch = task.get("branch")
            if branch:
                proc = await asyncio.create_subprocess_exec(
                    "git", "-C", bare_path, "branch", "-D", branch,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
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
    auto_test: bool = True,
    depends_on: str | None = None,
    auto_review: bool = True,
    review_model: str | None = None,
    parent_task_id: str | None = None,
    auto_pr: bool = False,
    component_id: str | None = None,
    claude_chat_url: str | None = None,
    auto_merge: bool = False,
    auto_release_worktree: bool = True,
    base_branch: str | None = None,
    held: bool = False,
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

    # Create or get task
    task = await db.get_task(task_id)
    is_resume = False

    if task is None:
        task = await db.create_task(
            id=task_id, project_id=project_id, goal=goal,
            branch=branch,
            max_turns=max_turns, max_wall_clock=max_wall_clock,
            jira_ticket=jira_ticket, conversation_id=conversation_id,
            model=model, auto_test=auto_test, depends_on=depends_on,
            auto_review=auto_review, review_model=review_model,
            parent_task_id=parent_task_id, auto_pr=auto_pr,
            component_id=component_id, claude_chat_url=claude_chat_url,
            auto_merge=auto_merge, auto_release_worktree=auto_release_worktree,
            base_branch=base_branch,
        )
        if spec:
            await db.post_task_message(
                task_id=task_id, author="dispatcher", content=spec,
                type="spec", title="Task Spec", pinned=True,
            )
        if checklist:
            await db.create_checklist_items(task_id, checklist)

        # Backward trigger: if depends_on parent hasn't passed gate yet, don't dispatch
        if depends_on:
            parent = await db.get_task(depends_on)
            if parent and not parent.get("gate_passed_at"):
                log.info(f"Task {task_id} waiting on parent {depends_on}")
                return {
                    "task_id": task_id, "status": "ready",
                    "waiting_on": depends_on,
                    "branch": task["branch"],
                    "queued": False,
                }
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
                                         depends_on=task.get("depends_on"))

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
    new_attempt = (task.get("current_attempt") or 1) + 1
    await db.update_task(task_id, session_id=None, gate_status=None, gate_passed_at=None,
                         current_attempt=new_attempt)

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

    return await dispatch_task(
        project_id=task["project_id"],
        task_id=task_id,
        goal=task["goal"],
        phase="revisions" if review_feedback else "analysis",
        review_feedback=review_feedback,
    )


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

    await db.update_task(task_id, status="cancelled")

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
    """Terminal status + optional worktree cleanup."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    project = await db.get_project(task["project_id"])

    # Archive logs before destroying the worktree
    if project:
        await archive_task_logs(task, project, "close")

    if cleanup and project:
        await cleanup_worktree(project, task, force_delete_branch)
        await db.update_task(
            task_id, status="completed", worktree_path=None,
        )
    else:
        await db.update_task(task_id, status="completed")

    return {"task_id": task_id, "status": "completed", "cleaned_up": cleanup}




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
