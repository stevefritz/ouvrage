"""Task execution engine — Agent SDK dispatch, worktree ops, lifecycle management."""

import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

import database as db

log = logging.getLogger("switchboard.tasks")


def _resolve_limit(task_val, project_val, global_default):
    """Resolve a limit: task override > project default > global default."""
    if task_val is not None:
        return task_val
    if project_val is not None:
        return project_val
    return global_default


# ---------------------------------------------------------------------------
# Git Worktree Management
# ---------------------------------------------------------------------------

WORKER_USER = "switchboard"


async def _run_as_worker(*cmd, **kwargs) -> tuple[bytes, bytes, int]:
    """Run a command as the worker user. Returns (stdout, stderr, returncode)."""
    proc = await asyncio.create_subprocess_exec(
        "su", "-", WORKER_USER, "-c", " ".join(cmd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    stdout, stderr = await proc.communicate()
    return stdout, stderr, proc.returncode


async def setup_worktree(project: dict, dir_name: str, branch: str) -> str:
    """Create git worktree for a task. Returns worktree path.

    Args:
        project: Project config dict.
        dir_name: Filesystem-safe directory name (no slashes).
        branch: Git branch name (may contain slashes like feature/foo).
    """
    base = project["working_dir"]
    worktree_path = os.path.join(base, dir_name)

    if os.path.exists(worktree_path):
        log.info(f"Worktree already exists: {worktree_path}")
        return worktree_path

    # Ensure base directory exists
    os.makedirs(base, exist_ok=True)
    # Ensure worker user owns it
    import shutil
    shutil.chown(base, user=WORKER_USER, group=WORKER_USER)

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

    # Create worktree
    default_branch = project["default_branch"]
    stdout, stderr, rc = await _run_as_worker(
        "git", "-C", bare_path, "worktree", "add",
        "-b", branch, worktree_path, f"origin/{default_branch}",
    )
    if rc != 0:
        # Branch might already exist, try without -b
        stdout, stderr, rc = await _run_as_worker(
            "git", "-C", bare_path, "worktree", "add",
            worktree_path, branch,
        )
        if rc != 0:
            raise RuntimeError(f"git worktree add failed: {stderr.decode()}")

    log.info(f"Created worktree: {worktree_path} on branch {branch}")
    return worktree_path


async def run_setup_command(project: dict, worktree_path: str, env_overrides: dict | None = None):
    """Run project setup command in the worktree."""
    cmd = project.get("setup_command")
    if not cmd:
        return

    # Write env overrides to .env.testing if provided
    overrides = env_overrides
    if not overrides and project.get("env_overrides"):
        overrides = project["env_overrides"]
        if isinstance(overrides, str):
            overrides = json.loads(overrides)

    if overrides:
        env_path = os.path.join(worktree_path, ".env.testing")
        with open(env_path, "w") as f:
            for k, v in overrides.items():
                f.write(f"{k}={v}\n")
        log.info(f"Wrote env overrides to {env_path}")

    log.info(f"Running setup: {cmd} in {worktree_path}")
    stdout, stderr, rc = await _run_as_worker("sh", "-c", f"'cd {worktree_path} && {cmd}'")
    if rc != 0:
        log.warning(f"Setup command failed (exit {rc}): {stderr.decode()}")


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

def _build_task_prompt(project: dict, task: dict, spec_content: str | None, escalation_criteria: str | None = None) -> str:
    """Build the prompt CC receives when dispatched."""
    parts = []

    parts.append(f"# Task: {task['goal']}")
    parts.append(f"Project: {project['id']} | Branch: {task['branch']}")
    parts.append(f"Task ID: {task['id']}")
    parts.append("")

    if spec_content:
        parts.append("## Spec")
        parts.append(spec_content)
        parts.append("")

    parts.append("## Instructions")
    parts.append("- You are working in an isolated git worktree. Commit freely to your branch.")
    parts.append("- Use the switchboard MCP tools to report progress:")
    parts.append(f"  - Update checklist: mcp__switchboard__update_task_checklist(item_id=N, done=true)")
    parts.append(f"  - Update phase: mcp__switchboard__update_task_phase(task_id='{task['id']}', phase='implementing', detail='...')")
    parts.append(f"  - Post progress: mcp__switchboard__post_task_message(task_id='{task['id']}', author='cc-worker', type='progress', content='...')")
    parts.append(f"  - Post question (will pause session): mcp__switchboard__post_task_message(task_id='{task['id']}', author='cc-worker', type='question', content='...')")
    parts.append("- Update checklist items as you complete them.")
    parts.append("- When done, commit your work and post a result summary as type='result'.")
    parts.append("")

    if project.get("test_command"):
        parts.append(f"## Test Command")
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

def _setup_log_dir(worktree_path: str) -> Path:
    """Create .switchboard log directory in the worktree."""
    log_dir = Path(worktree_path) / ".switchboard"
    log_dir.mkdir(exist_ok=True)
    return log_dir


def _write_dispatch_log(log_dir: Path, task_id: str, session_id: str,
                        max_turns: int, max_wall_clock: int,
                        worktree_path: str, is_resume: bool):
    """Write dispatch metadata to log file."""
    log_path = log_dir / "dispatch.log"
    with open(log_path, "a") as f:
        f.write(f"[{db.now_iso()}] {'Resuming' if is_resume else 'Dispatching'} task {task_id}\n")
        f.write(f"  session_id: {session_id}\n")
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
    log_dir: Path,
) -> None:
    """Run a CC session via the Agent SDK. Blocks until complete."""
    stderr_path = log_dir / "cc-stderr.log"
    stderr_log = open(stderr_path, "a")

    # Build SDK options — run CC as restricted 'switchboard' user
    options = ClaudeAgentOptions(
        user="switchboard",
        cwd=str(worktree_path),
        allowed_tools=[
            "Read", "Write", "Edit", "Bash", "Glob", "Grep",
            "mcp__switchboard__update_task_checklist",
            "mcp__switchboard__update_task_phase",
            "mcp__switchboard__post_task_message",
            "mcp__switchboard__read_task_messages",
            "mcp__switchboard__get_task_status",
        ],
        permission_mode="bypassPermissions",
        max_turns=max_turns,
        setting_sources=["project"],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": prompt if not is_resume else "",
        },
        mcp_servers={
            "switchboard": {
                "type": "http",
                "url": "http://localhost:8100/mcp",
            },
        },
        debug_stderr=stderr_log,
    )

    # If resuming, use the resume option
    if is_resume and session_id:
        options.resume = session_id

    try:
        result_msg = None
        timeout_seconds = max_wall_clock_minutes * 60

        async def _run():
            nonlocal result_msg
            actual_prompt = _build_resume_prompt({"id": task_id}) if is_resume else prompt
            async for message in query(prompt=actual_prompt, options=options):
                if isinstance(message, ResultMessage):
                    result_msg = message
                    # Capture session_id from first dispatch
                    if message.session_id:
                        await db.update_task(task_id, session_id=message.session_id)

                # Update last_activity on each message
                await db.update_task(task_id, last_activity=db.now_iso())

        try:
            await asyncio.wait_for(_run(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            log.warning(f"Task {task_id}: wall clock timeout ({max_wall_clock_minutes}m)")
            await db.update_task(task_id, status="needs-review", pid=None)
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title="Wall clock timeout",
                content=f"Task hit the {max_wall_clock_minutes} minute wall clock limit. "
                        "Work is preserved in the worktree. Resume or adjust limits.",
            )
            with open(log_dir / "dispatch.log", "a") as f:
                f.write(f"[{db.now_iso()}] Wall clock timeout ({max_wall_clock_minutes}m)\n")
            return

        # Process result
        if result_msg:
            _log_result(log_dir, result_msg)
            await _update_usage(task_id, result_msg)

            if result_msg.is_error:
                await db.update_task(task_id, status="failed", pid=None)
                await db.post_task_message(
                    task_id=task_id, author="dispatcher", type="status",
                    title="Task failed",
                    content=f"CC session ended with error.\n\nStop reason: {result_msg.stop_reason}\n"
                            f"Turns: {result_msg.num_turns}\n\n"
                            f"Result: {result_msg.result or '(no result)'}",
                )
            else:
                await db.update_task(task_id, status="completed", pid=None)
                await db.post_task_message(
                    task_id=task_id, author="dispatcher", type="status",
                    title="Task completed",
                    content=f"CC session completed successfully.\n\n"
                            f"Turns: {result_msg.num_turns} | "
                            f"Duration: {result_msg.duration_ms / 1000:.0f}s | "
                            f"Cost: ${result_msg.total_cost_usd or 0:.4f}\n\n"
                            f"Result: {(result_msg.result or '(no result)')[:500]}",
                )
        else:
            # No result message — shouldn't happen but handle gracefully
            await db.update_task(task_id, status="needs-review", pid=None)
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title="Session ended without result",
                content="CC session ended but no ResultMessage was received. Check logs.",
            )

    except Exception as e:
        log.exception(f"SDK session error for task {task_id}: {e}")
        await db.update_task(task_id, status="failed", pid=None)
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Dispatch error",
            content=f"SDK session raised an exception:\n\n```\n{e}\n```",
        )
        with open(log_dir / "dispatch.log", "a") as f:
            f.write(f"[{db.now_iso()}] SDK error: {e}\n")
    finally:
        stderr_log.close()


def _log_result(log_dir: Path, result: ResultMessage):
    """Write result metadata to dispatch log."""
    with open(log_dir / "dispatch.log", "a") as f:
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
# Public Task Operations
# ---------------------------------------------------------------------------

async def dispatch_task(
    project_id: str, task_id: str, goal: str,
    spec: str | None = None, checklist: list[str] | None = None,
    phase: str = "analysis", max_turns: int | None = None,
    max_wall_clock: int | None = None,
    escalation_criteria: str | None = None,
    branch: str | None = None,
) -> dict:
    """Create task (if needed), setup worktree, launch CC via Agent SDK."""

    # Check concurrency limit
    active = await db.count_active_tasks()
    if active >= db.DEFAULT_MAX_CONCURRENT:
        raise RuntimeError(
            f"Concurrency limit reached ({active}/{db.DEFAULT_MAX_CONCURRENT} active tasks). "
            "Cancel or wait for a task to finish."
        )

    # Get project
    project = await db.get_project(project_id)
    if not project:
        raise ValueError(f"Project '{project_id}' not found. Register it with create_project first.")

    # Create or get task
    task = await db.get_task(task_id)
    is_resume = False

    if task is None:
        task = await db.create_task(
            id=task_id, project_id=project_id, goal=goal,
            branch=branch,
            max_turns=max_turns, max_wall_clock=max_wall_clock,
        )
        if spec:
            await db.post_task_message(
                task_id=task_id, author="dispatcher", content=spec,
                type="spec", title="Task Spec", pinned=True,
            )
        if checklist:
            await db.create_checklist_items(task_id, checklist)
    elif task["status"] == "needs-review":
        is_resume = True
    elif task["status"] == "working":
        if task.get("pid") and _is_pid_alive(task["pid"]):
            raise RuntimeError(f"Task '{task_id}' is already running (PID {task['pid']})")
        is_resume = True

    # Setup worktree — dir_name is always filesystem-safe (no slashes)
    # Branch may contain slashes (e.g. feature/foo)
    effective_branch = task["branch"] or task_id
    short_name = task_id.split("/")[-1]
    dir_name = short_name
    worktree_path = await setup_worktree(project, dir_name, effective_branch)

    # Run setup command
    await run_setup_command(project, worktree_path)

    # Setup logging
    log_dir = _setup_log_dir(worktree_path)

    # Resolve limits
    effective_max_turns = _resolve_limit(
        task.get("max_turns"), project.get("max_turns"), db.DEFAULT_MAX_TURNS
    )
    effective_max_wall_clock = _resolve_limit(
        task.get("max_wall_clock"), project.get("max_wall_clock"), db.DEFAULT_MAX_WALL_CLOCK
    )

    # Build prompt
    spec_content = None
    pinned = await db.get_task_pinned(task_id)
    if pinned:
        spec_content = pinned["content"]

    prompt = _build_task_prompt(project, task, spec_content, escalation_criteria)

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
        worktree_path, is_resume,
    )

    # Launch SDK session in background — non-blocking
    asyncio.create_task(_run_sdk_session(
        task_id=task_id,
        prompt=prompt,
        worktree_path=worktree_path,
        session_id=session_id,
        is_resume=is_resume,
        max_turns=effective_max_turns,
        max_wall_clock_minutes=effective_max_wall_clock,
        log_dir=log_dir,
    ))

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
        "resumed": is_resume,
    }


async def resume_task(task_id: str) -> dict:
    """Resume a paused task with the same session ID."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")
    if task["status"] != "needs-review":
        raise ValueError(f"Task '{task_id}' is in status '{task['status']}', expected 'needs-review'")

    return await dispatch_task(
        project_id=task["project_id"],
        task_id=task_id,
        goal=task["goal"],
        phase=task.get("phase") or "implementing",
    )


async def retry_task(task_id: str, clean: bool = False) -> dict:
    """Start a fresh session. Optionally clean worktree."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    # Kill if still running
    if task.get("pid") and _is_pid_alive(task["pid"]):
        _kill_pid(task["pid"])

    # Clear session to force new one
    await db.update_task(task_id, session_id=None, pid=None)

    # Optionally clean worktree
    if clean and task.get("worktree_path") and os.path.exists(task["worktree_path"]):
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", task["worktree_path"], "checkout", ".",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    return await dispatch_task(
        project_id=task["project_id"],
        task_id=task_id,
        goal=task["goal"],
        phase="analysis",
    )


async def cancel_task(task_id: str) -> dict:
    """Kill a running task."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    if task.get("pid") and _is_pid_alive(task["pid"]):
        _kill_pid(task["pid"])

    await db.update_task(task_id, status="cancelled", pid=None)
    return {"task_id": task_id, "status": "cancelled"}


async def close_task(task_id: str, cleanup: bool = True, force_delete_branch: bool = False) -> dict:
    """Terminal status + optional worktree cleanup."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    # Kill if still running
    if task.get("pid") and _is_pid_alive(task["pid"]):
        _kill_pid(task["pid"])

    project = await db.get_project(task["project_id"])

    if cleanup and project:
        await cleanup_worktree(project, task, force_delete_branch)
        await db.update_task(
            task_id, status="completed", pid=None, worktree_path=None,
        )
    else:
        await db.update_task(task_id, status="completed", pid=None)

    return {"task_id": task_id, "status": "completed", "cleaned_up": cleanup}


# ---------------------------------------------------------------------------
# Process Management Helpers
# ---------------------------------------------------------------------------

def _is_pid_alive(pid: int) -> bool:
    """Check if a process is still running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _kill_pid(pid: int):
    """Send SIGTERM to a process."""
    try:
        os.kill(pid, signal.SIGTERM)
        log.info(f"Sent SIGTERM to PID {pid}")
    except (OSError, ProcessLookupError):
        log.info(f"PID {pid} already dead")
