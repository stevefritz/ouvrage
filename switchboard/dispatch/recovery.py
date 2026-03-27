"""switchboard.dispatch.recovery — crash recovery and stall detection.

Handles three categories of broken tasks after a service restart:
  1. Orphaned CC sessions (status=working, no live PID) → auto-resume or retry
  2. Gate subtasks (parent_task_id set) → re-trigger parent gate
  3. Gate pipeline stuck (test-failed/review-failed) → re-trigger gate

Also provides check_stalled_tasks() — a background loop that detects working
tasks with no recent activity or dead SDK clients.

Lazy imports from dispatch siblings (to break circular dependency):
  engine: resume_task, retry_task
  gates: _run_test_gate, _dispatch_review
  _state: _active_clients
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

import switchboard.db as db
from switchboard.notifications import slack as notify
from switchboard.config.settings import (
    RECOVERY_STAGGER_SECONDS,
    MAX_RECOVERY_ATTEMPTS,
    RECOVERY_ENABLED,
)
from switchboard.config.constants import STALL_THRESHOLD_SECONDS, STALL_CHECK_INTERVAL

log = logging.getLogger(__name__)


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is running by PID."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


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
    # Lazy imports to break circular dependency with dispatch.engine
    from switchboard.dispatch.gates import _run_test_gate, _dispatch_review  # noqa: PLC0415

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
    # Lazy imports to break circular dependency
    from switchboard.dispatch.gates import _run_test_gate, _dispatch_review  # noqa: PLC0415

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
    # Lazy import to break circular dependency
    from switchboard.dispatch.engine import resume_task  # noqa: PLC0415

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
    # Lazy import to break circular dependency
    from switchboard.dispatch.engine import retry_task  # noqa: PLC0415

    # Task is already in needs-review status (set at start of recovery)
    try:
        await retry_task(task_id)
        log.info(f"Recovery: retried {task_id} with fresh session")
    except Exception as e:
        log.warning(f"Recovery: retry failed for {task_id}: {e}")
        await db.update_task(task_id, status="needs-review")
        raise


async def _recover_single_task(task: dict):
    """Attempt to recover a single dead/orphaned task."""
    # Lazy imports to break circular dependency
    from switchboard.dispatch.engine import resume_task, retry_task  # noqa: PLC0415

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


async def check_stalled_tasks():
    """Background loop: check for working tasks with no recent activity or dead processes."""
    # Lazy imports to break circular dependency
    from switchboard.dispatch.engine import retry_task  # noqa: PLC0415
    from switchboard.dispatch._state import _active_clients

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
                    # Skip held tasks — they require manual approval before dispatch
                    if task.get("held"):
                        log.info(f"Health check: skipping held task {task['id']} — requires manual approval")
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
