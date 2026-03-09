"""Task execution engine — subprocess management, worktree ops, dispatch."""

import asyncio
import json
import logging
import os
import signal
import uuid
from pathlib import Path

import database as db

log = logging.getLogger("switchboard.tasks")


def _resolve_limit(task_val, project_val, global_default):
    """Resolve a limit: task override > project default > global default."""
    if task_val is not None:
        return task_val
    if project_val is not None:
        return project_val
    return global_default


async def setup_worktree(project: dict, task_id: str, branch: str) -> str:
    """Create git worktree for a task. Returns worktree path."""
    base = project["working_dir"]
    worktree_path = os.path.join(base, task_id)

    if os.path.exists(worktree_path):
        log.info(f"Worktree already exists: {worktree_path}")
        return worktree_path

    # Ensure base directory exists
    os.makedirs(base, exist_ok=True)

    # Clone the repo as a bare repo if the base doesn't have .git
    bare_path = os.path.join(base, ".bare")
    if not os.path.exists(bare_path):
        log.info(f"Cloning bare repo: {project['repo']} -> {bare_path}")
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--bare", project["repo"], bare_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git clone --bare failed: {stderr.decode()}")

    # Fetch latest from remote
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", bare_path, "fetch", "origin",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    # Create worktree
    default_branch = project["default_branch"]
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", bare_path, "worktree", "add",
        "-b", branch, worktree_path, f"origin/{default_branch}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        # Branch might already exist, try without -b
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", bare_path, "worktree", "add",
            worktree_path, branch,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
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
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=worktree_path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.warning(f"Setup command failed (exit {proc.returncode}): {stderr.decode()}")
        # Don't raise — setup failures shouldn't block dispatch entirely
        # CC can handle missing deps in its session


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


def _build_system_prompt(project: dict, task: dict, spec_content: str | None, escalation_criteria: str | None = None) -> str:
    """Build the system prompt CC receives when dispatched."""
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
    parts.append("- Use the switchboard MCP tools on localhost:8100 to report progress:")
    parts.append(f"  - Update checklist: update_task_checklist(task_id='{task['id']}', ...)")
    parts.append(f"  - Update phase: update_task_phase(task_id='{task['id']}', phase='implementing', detail='...')")
    parts.append(f"  - Post progress: post_task_message(task_id='{task['id']}', type='progress', ...)")
    parts.append(f"  - Post question (blocks until answered): post_task_message(task_id='{task['id']}', type='question', ...)")
    parts.append("- When you post a message with type='question', your session will be paused until someone answers.")
    parts.append("- Update checklist items as you complete them.")
    parts.append("- When done, commit your work and post a result summary.")
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


def _setup_log_dir(worktree_path: str) -> Path:
    """Create .switchboard log directory in the worktree."""
    log_dir = Path(worktree_path) / ".switchboard"
    log_dir.mkdir(exist_ok=True)
    return log_dir


async def dispatch_task(
    project_id: str, task_id: str, goal: str,
    spec: str | None = None, checklist: list[str] | None = None,
    phase: str = "analysis", max_turns: int | None = None,
    max_wall_clock: int | None = None,
    escalation_criteria: str | None = None,
) -> dict:
    """Create task (if needed), setup worktree, fork CC subprocess."""

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
            max_turns=max_turns, max_wall_clock=max_wall_clock,
        )
        # Post spec as pinned message
        if spec:
            await db.post_task_message(
                task_id=task_id, author="dispatcher", content=spec,
                type="spec", title="Task Spec", pinned=True,
            )
        # Create checklist items
        if checklist:
            await db.create_checklist_items(task_id, checklist)
    elif task["status"] == "needs-review":
        is_resume = True
    elif task["status"] == "working":
        # Check if PID is actually alive
        if task.get("pid") and _is_pid_alive(task["pid"]):
            raise RuntimeError(f"Task '{task_id}' is already running (PID {task['pid']})")
        # PID dead but status stuck — recover
        is_resume = True

    # Setup worktree
    branch = task["branch"] or task_id
    worktree_path = await setup_worktree(project, task_id, branch)

    # Run setup command
    await run_setup_command(project, worktree_path)

    # Setup logging
    log_dir = _setup_log_dir(worktree_path)

    # Resolve session ID
    session_id = task.get("session_id")
    if not session_id or not is_resume:
        session_id = str(uuid.uuid4())

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

    prompt = _build_system_prompt(project, task, spec_content, escalation_criteria)

    # Update task record
    dispatch_count = (task.get("dispatch_count") or 0) + 1
    await db.update_task(
        task_id,
        status="working",
        phase=phase,
        worktree_path=worktree_path,
        session_id=session_id,
        dispatch_count=dispatch_count,
        last_activity=db.now_iso(),
    )

    # Fork CC subprocess
    pid = await _launch_cc_subprocess(
        task_id=task_id,
        worktree_path=worktree_path,
        prompt=prompt,
        session_id=session_id,
        max_turns=effective_max_turns,
        max_wall_clock_minutes=effective_max_wall_clock,
        log_dir=log_dir,
    )

    await db.update_task(task_id, pid=pid)

    return {
        "task_id": task_id,
        "status": "working",
        "phase": phase,
        "worktree_path": worktree_path,
        "branch": branch,
        "session_id": session_id,
        "pid": pid,
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


async def _launch_cc_subprocess(
    task_id: str, worktree_path: str, prompt: str, session_id: str,
    max_turns: int, max_wall_clock_minutes: int, log_dir: Path,
) -> int:
    """Launch CC as a subprocess via claude CLI. Returns PID."""
    stdout_log = open(log_dir / "cc-stdout.log", "a")
    stderr_log = open(log_dir / "cc-stderr.log", "a")
    dispatch_log = open(log_dir / "dispatch.log", "a")

    dispatch_log.write(f"[{db.now_iso()}] Dispatching task {task_id}\n")
    dispatch_log.write(f"  session_id: {session_id}\n")
    dispatch_log.write(f"  max_turns: {max_turns}\n")
    dispatch_log.write(f"  max_wall_clock: {max_wall_clock_minutes}m\n")
    dispatch_log.write(f"  worktree: {worktree_path}\n")
    dispatch_log.flush()

    # Write prompt to a temp file to avoid shell escaping hell
    prompt_file = log_dir / "prompt.md"
    prompt_file.write_text(prompt)

    # Build claude CLI command
    # Using claude -p (print/pipe mode) with --max-turns
    cmd = [
        "claude",
        "-p", prompt,
        "--max-turns", str(max_turns),
        "--output-format", "json",
        "--verbose",
    ]

    # TODO: When Agent SDK is validated, switch to SDK-based launch
    # with session_id support for pause/resume. For now, CLI mode
    # doesn't support session persistence — each dispatch is a fresh session.
    # The worktree preserves code state between dispatches even without
    # session persistence.

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=worktree_path,
        stdout=stdout_log,
        stderr=stderr_log,
        start_new_session=True,  # Detach from our process group
    )

    dispatch_log.write(f"  pid: {proc.pid}\n")
    dispatch_log.flush()

    # Start background monitor for exit handling + wall clock timeout
    asyncio.create_task(_monitor_subprocess(
        proc, task_id, max_wall_clock_minutes, dispatch_log, stdout_log, stderr_log,
    ))

    return proc.pid


async def _monitor_subprocess(
    proc: asyncio.subprocess.Process, task_id: str,
    max_wall_clock_minutes: int,
    dispatch_log, stdout_log, stderr_log,
):
    """Monitor CC subprocess — handle exit, enforce wall clock timeout."""
    try:
        timeout_seconds = max_wall_clock_minutes * 60
        try:
            return_code = await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            # Wall clock timeout
            dispatch_log.write(f"[{db.now_iso()}] Wall clock timeout ({max_wall_clock_minutes}m). Sending SIGTERM.\n")
            dispatch_log.flush()

            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                dispatch_log.write(f"[{db.now_iso()}] SIGTERM timeout. Sending SIGKILL.\n")
                dispatch_log.flush()
                proc.kill()
                await proc.wait()

            await db.update_task(task_id, status="needs-review", pid=None)
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title="Wall clock timeout",
                content=f"Task hit the {max_wall_clock_minutes} minute wall clock limit. "
                        "Work is preserved in the worktree. Resume or adjust limits.",
            )
            return

        dispatch_log.write(f"[{db.now_iso()}] CC exited with code {return_code}\n")
        dispatch_log.flush()

        # Parse output for token usage if available
        await _capture_usage(task_id, stdout_log.name)

        if return_code == 0:
            await db.update_task(task_id, status="completed", pid=None)
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title="Task completed",
                content="CC session ended successfully.",
            )
        else:
            await db.update_task(task_id, status="failed", pid=None)
            # Try to capture last few lines of stderr for context
            error_context = _tail_file(stderr_log.name, 20)
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title=f"Task failed (exit code {return_code})",
                content=f"CC exited with code {return_code}.\n\n```\n{error_context}\n```",
            )
    except Exception as e:
        log.exception(f"Monitor error for task {task_id}: {e}")
        await db.update_task(task_id, status="failed", pid=None)
    finally:
        for f in (dispatch_log, stdout_log, stderr_log):
            try:
                f.close()
            except Exception:
                pass


async def _capture_usage(task_id: str, stdout_path: str):
    """Try to parse token usage from CC's JSON output."""
    try:
        content = Path(stdout_path).read_text()
        # CC with --output-format json outputs a JSON object
        # Look for the last valid JSON block
        lines = content.strip().split("\n")
        for line in reversed(lines):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                    input_tokens = data.get("input_tokens", 0)
                    output_tokens = data.get("output_tokens", 0)
                    cost = data.get("cost_usd", 0.0)

                    if input_tokens or output_tokens:
                        task = await db.get_task(task_id)
                        await db.update_task(
                            task_id,
                            total_input_tokens=(task.get("total_input_tokens") or 0) + input_tokens,
                            total_output_tokens=(task.get("total_output_tokens") or 0) + output_tokens,
                            total_cost_usd=(task.get("total_cost_usd") or 0.0) + cost,
                        )
                    break
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        log.warning(f"Could not capture usage for task {task_id}: {e}")


def _tail_file(path: str, n: int = 20) -> str:
    """Read last N lines from a file."""
    try:
        with open(path) as f:
            lines = f.readlines()
            return "".join(lines[-n:])
    except Exception:
        return "(could not read log)"
