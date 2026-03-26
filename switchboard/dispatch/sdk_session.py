"""switchboard.dispatch.sdk_session — Claude Agent SDK session management.

Handles everything needed to run a CC worker session:
  - Prompt building (_build_task_prompt, _build_resume_prompt)
  - Log directory setup and shared-file utilities
  - The main SDK dispatch loop (_run_sdk_session)
  - Result logging (_log_result)

Also applies the anyio process isolation patch at module import time so that
all CC subprocess spawns get their own session/process group.

Lazy imports from tasks (to break circular dependency):
  _run_test_gate, _dispatch_review, _process_review_result,
  _check_and_dispatch_dependents, _drain_queue, _update_usage, _active_clients
"""

import asyncio
import json
import logging
import os
import pwd
import re
import time
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
from switchboard.notifications import slack as notify
from switchboard.config.settings import WORKER_USER
from switchboard.config.constants import MESSAGE_POLL_INTERVAL, DEFAULT_MODEL
from switchboard.git.worktree import _run_as_worker
from switchboard.git.operations import _ensure_branch_pushed

log = logging.getLogger("switchboard.tasks")

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

    parts.append("## Worktree hygiene — required before handoff")
    parts.append("")
    parts.append("Before posting your result, run `git status`. Your worktree MUST be clean.")
    parts.append("")
    parts.append("For every file that shows as modified, staged, or untracked:")
    parts.append("")
    parts.append("- **If you changed it intentionally as part of this task:** stage and commit it with a meaningful message that describes what changed and why. Do not batch unrelated changes into one commit.")
    parts.append("- **If it changed but you did NOT intend to change it** (e.g. a file got touched by a side effect, a config was auto-modified): run `git checkout -- <file>` to revert it to its original state.")
    parts.append("")
    parts.append("There is no option to leave changes uncommitted. You own this worktree. The next step in the pipeline (tests, reviewer) operates on committed code only. An uncommitted implementation is an incomplete task.")
    parts.append("")
    parts.append("Do NOT create garbage commits like \"fix formatting\" or \"clean up\" unless formatting or cleanup was explicitly part of the spec. Commit messages must describe the actual change.")
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
# Logging utilities
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
    # Lazy imports to break circular dependency with tasks.py
    from tasks import (  # noqa: PLC0415
        _run_test_gate, _dispatch_review, _process_review_result,
        _check_and_dispatch_dependents, _drain_queue, _update_usage,
        _active_clients,
    )

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
