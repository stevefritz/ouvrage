"""Task execution engine — Agent SDK dispatch, worktree ops, lifecycle management."""

import asyncio
import json
import logging
import os
import pwd
import shlex
import time
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    UserMessage,
    query,
)
from claude_agent_sdk.types import TextBlock, ToolUseBlock, ToolResultBlock

import database as db
import notifications as notify

log = logging.getLogger("switchboard.tasks")

# Track running async tasks to prevent garbage collection and silent failures
_running_tasks: set[asyncio.Task] = set()


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
# Git Worktree Management
# ---------------------------------------------------------------------------

WORKER_USER = "switchboard"


def _get_worker_ids() -> tuple[int, int]:
    """Get uid/gid for the worker user."""
    pw = pwd.getpwnam(WORKER_USER)
    return pw.pw_uid, pw.pw_gid


async def _run_as_worker(*cmd, **kwargs) -> tuple[bytes, bytes, int]:
    """Run a command as the worker user via setuid (requires CAP_SETUID)."""
    uid, gid = _get_worker_ids()

    def _demote():
        os.setgid(gid)
        os.setuid(uid)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        preexec_fn=_demote,
        **kwargs,
    )
    stdout, stderr = await proc.communicate()
    return stdout, stderr, proc.returncode


async def setup_worktree(project: dict, task_id: str, branch: str) -> str:
    """Create git worktree for a task. Returns worktree path."""
    base = project["working_dir"]
    worktree_path = os.path.join(base, task_id)

    if os.path.exists(worktree_path):
        log.info(f"Worktree already exists: {worktree_path}")
        return worktree_path

    # Ensure base directory exists (worker user should already own it)
    os.makedirs(base, exist_ok=True)

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
        "-b", branch, worktree_path, default_branch,
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
    stdout, stderr, rc = await _run_as_worker("sh", "-c", f"cd {shlex.quote(worktree_path)} && {cmd}")
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

def _build_task_prompt(project: dict, task: dict, spec_content: str | None,
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

async def _setup_log_dir(worktree_path: str) -> Path:
    """Create .switchboard log directory in the worktree.

    Created as the worker user (who owns the worktree), with group-write
    so the service user can also write dispatch/session logs.
    """
    log_dir = Path(worktree_path) / ".switchboard"
    await _run_as_worker("mkdir", "-p", str(log_dir))
    await _run_as_worker("chmod", "775", str(log_dir))
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
    old_umask = os.umask(0o002)  # Files group-writable (shared switchboard group)
    stderr_log = open(stderr_path, "a")
    os.umask(old_umask)

    # Build SDK options — run CC as restricted 'switchboard' user
    worker_home = pwd.getpwnam(WORKER_USER).pw_dir
    options = ClaudeAgentOptions(
        user="switchboard",
        cwd=str(worktree_path),
        env={"HOME": worker_home},
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
                            entry["content"].append({"type": "text", "text": block.text[:2000]})
                        elif isinstance(block, ToolUseBlock):
                            entry["content"].append({
                                "type": "tool_use", "name": block.name,
                                "input": str(block.input)[:1000],
                            })
                    entry["stop_reason"] = getattr(msg, "stop_reason", None)
                    entry["model"] = getattr(msg, "model", None)
                elif isinstance(msg, UserMessage):
                    entry["content"] = []
                    content = msg.content
                    if isinstance(content, str):
                        entry["content"].append({"type": "text", "text": content[:2000]})
                    else:
                        for block in (content or []):
                            if isinstance(block, ToolResultBlock):
                                entry["content"].append({
                                    "type": "tool_result",
                                    "tool_use_id": block.tool_use_id,
                                    "preview": str(block.content or "")[:500],
                                    "is_error": getattr(block, "is_error", None),
                                })
                elif isinstance(msg, ResultMessage):
                    entry["subtype"] = getattr(msg, "subtype", None)
                    entry["result"] = (msg.result or "")[:1000]
                    entry["num_turns"] = msg.num_turns
                    entry["session_id"] = getattr(msg, "session_id", None)
                    entry["cost_usd"] = msg.total_cost_usd
                    entry["duration_ms"] = getattr(msg, "duration_ms", None)
                    entry["is_error"] = getattr(msg, "is_error", None)
                with open(session_log_path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception as e:
                log.warning(f"Failed to log message: {e}")

        async def _run():
            nonlocal result_msg
            start_time = time.monotonic()
            last_heartbeat = start_time
            heartbeat_interval = 90  # seconds
            turn_count = 0
            running_cost = 0.0
            last_tool_name = None

            actual_prompt = _build_resume_prompt({"id": task_id}) if is_resume else prompt
            async for message in query(prompt=actual_prompt, options=options):
                _log_message(message)

                if isinstance(message, AssistantMessage):
                    turn_count += 1
                    # Track last tool used for heartbeat context
                    for block in (message.content or []):
                        if isinstance(block, ToolUseBlock):
                            last_tool_name = block.name

                if isinstance(message, ResultMessage):
                    result_msg = message
                    # Capture session_id from first dispatch
                    if message.session_id:
                        await db.update_task(task_id, session_id=message.session_id)
                    running_cost = message.total_cost_usd or 0

                # Heartbeat: post to Slack every N seconds
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
            with open(log_dir / "dispatch.log", "a") as f:
                f.write(f"[{db.now_iso()}] Wall clock timeout ({max_wall_clock_minutes}m)\n")
            return

        # Process result
        if result_msg:
            _log_result(log_dir, result_msg)
            await _update_usage(task_id, result_msg)

            if result_msg.is_error:
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
            else:
                await db.update_task(task_id, status="completed")
                await db.post_task_message(
                    task_id=task_id, author="dispatcher", type="status",
                    title="Task completed",
                    content=f"CC session completed successfully.\n\n"
                            f"Turns: {result_msg.num_turns} | "
                            f"Duration: {result_msg.duration_ms / 1000:.0f}s | "
                            f"Cost: ${result_msg.total_cost_usd or 0:.4f}\n\n"
                            f"Result: {(result_msg.result or '(no result)')[:500]}",
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
        log.exception(f"SDK session error for task {task_id}: {e}")
        await db.update_task(task_id, status="failed")
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="status",
            title="Dispatch error",
            content=f"SDK session raised an exception:\n\n```\n{e}\n```",
        )
        await notify.task_failed(task_id=task_id, error=str(e))
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
    review_feedback: list[dict] | None = None,
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
        raise RuntimeError(f"Task '{task_id}' is already running")

    # Setup worktree — use short name (after project prefix) for branch and dir
    short_name = task_id.split("/")[-1] if "/" in task_id else task_id
    branch = task["branch"] or short_name
    if task["branch"] != branch:
        await db.update_task(task_id, branch=branch)
    worktree_path = await setup_worktree(project, short_name, branch)

    # Run setup command
    await run_setup_command(project, worktree_path)

    # Setup logging
    log_dir = await _setup_log_dir(worktree_path)

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

    # Fetch checklist items with IDs so CC knows how to update them
    checklist_items = await db.get_checklist(task_id)

    prompt = _build_task_prompt(project, task, spec_content, checklist_items, escalation_criteria, review_feedback)

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

    return {
        "task_id": task_id,
        "status": "working",
        "phase": phase,
        "worktree_path": worktree_path,
        "branch": branch,
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
    """Start a fresh session. Optionally clean worktree.

    If review/feedback messages were posted after the last CC result,
    they are injected into the prompt so CC knows to apply revisions.
    """
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    # Clear session to force new one
    await db.update_task(task_id, session_id=None)

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
    """Kill a running task."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    await db.update_task(task_id, status="cancelled")
    return {"task_id": task_id, "status": "cancelled"}


async def close_task(task_id: str, cleanup: bool = True, force_delete_branch: bool = False) -> dict:
    """Terminal status + optional worktree cleanup."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    project = await db.get_project(task["project_id"])

    if cleanup and project:
        await cleanup_worktree(project, task, force_delete_branch)
        await db.update_task(
            task_id, status="completed", worktree_path=None,
        )
    else:
        await db.update_task(task_id, status="completed")

    return {"task_id": task_id, "status": "completed", "cleaned_up": cleanup}
