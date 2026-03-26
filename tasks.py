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

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    UserMessage,
)
from claude_agent_sdk.types import TextBlock, ToolUseBlock

import database as db
from switchboard.notifications import slack as notify

# Process group isolation patch (anyio.open_process) is applied at import time
# in switchboard/dispatch/sdk_session.py — imported below.

log = logging.getLogger("switchboard.tasks")

# Track running async tasks to prevent garbage collection and silent failures
_running_tasks: set[asyncio.Task] = set()

# Track active SDK clients for tasks (used by cancel to interrupt)
_active_clients: dict[str, ClaudeSDKClient] = {}

from switchboard.config.constants import DEFAULT_MODEL


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
# Crash Recovery (extracted to switchboard/dispatch/recovery.py)
# ---------------------------------------------------------------------------

from switchboard.dispatch.recovery import (
    _is_pid_alive,
    mark_working_for_recovery,
    _classify_orphan,
    _classify_with_dependents,
    _verify_worktree,
    _build_recovery_message,
    recover_orphaned_tasks,
    _recover_task,
    _recover_gate_subtask,
    _recover_with_resume,
    _recover_with_retry,
    _recover_single_task,
    check_stalled_tasks,
)

# Re-export recovery constants so existing references (tests, server.py) keep working
from switchboard.config.settings import (
    RECOVERY_STAGGER_SECONDS,
    MAX_RECOVERY_ATTEMPTS,
    RECOVERY_ENABLED,
)


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
# Git (imported from switchboard.git)
# ---------------------------------------------------------------------------

from switchboard.config.settings import WORKER_USER
from switchboard.git.worktree import (
    _get_worker_ids,
    _run_as_worker,
    _find_branch_holder,
    setup_worktree,
    cleanup_worktree,
    run_setup_command,
)
from switchboard.git.operations import (
    resolve_branch_target,
    _git_fetch_and_rebase,
    _sync_branch_with_base,
    _ensure_branch_pushed,
    _get_branch_diff,
    _filter_diff_by_ignore_patterns,
    _maybe_create_pr,
    _perform_auto_merge,
)



# ---------------------------------------------------------------------------
# SDK Session + Prompt Building (extracted to switchboard/dispatch/sdk_session.py)
# ---------------------------------------------------------------------------

from switchboard.dispatch.sdk_session import (
    _build_task_prompt,
    _build_resume_prompt,
    _setup_log_dir,
    _open_shared,
    _write_dispatch_log,
    _tail_file,
    _run_sdk_session,
    _log_result,
)

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
            # Check for uncommitted changes before invoking reviewer
            worktree = task.get("worktree_path")
            if worktree:
                stdout, _stderr, _rc = await _run_as_worker(
                    "git", "-C", worktree, "status", "--porcelain"
                )
                if stdout.decode(errors="replace").strip():
                    # Dirty worktree — resume CC session with cleanup instruction
                    await db.post_task_message(
                        task_id=task_id,
                        author="switchboard",
                        type="status",
                        title="⚠️ Uncommitted changes — cleanup required",
                        content=(
                            "Your worktree has uncommitted changes. The reviewer cannot run until the worktree is clean.\n\n"
                            "Run `git status` and for each changed file:\n"
                            "- Commit it if the change is intentional and part of this task\n"
                            "- Revert it with `git checkout -- <file>` if it was unintentional\n\n"
                            "Post your result again when the worktree is clean."
                        ),
                    )
                    # Resume the same CC session (same attempt) — do NOT increment attempt
                    await resume_task(task_id)
                    return  # Do not proceed to reviewer
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


from switchboard.config.constants import (
    _DEFAULT_REVIEW_IGNORE_PATTERNS,
    _TAG_REVIEW_GUIDANCE,
    _DEFAULT_REVIEW_GUIDANCE,
)


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

    # --- Prior review history ---
    prior_reviews_thread = await db.read_task_messages(task_id, type="review")
    prior_review_msgs = [
        m for m in prior_reviews_thread.get("messages", [])
        if m.get("author") == "cc-worker"
    ]
    # Sort by attempt_number ascending so history is chronological
    prior_review_msgs.sort(key=lambda m: m.get("attempt_number") or 0)
    # Exclude reviews from the current attempt (only show prior attempts)
    current_attempt = task.get("current_attempt") or 1
    prior_review_msgs = [m for m in prior_review_msgs if (m.get("attempt_number") or 0) < current_attempt]

    prior_review_section = ""
    if prior_review_msgs:
        review_history_lines = [
            "## Prior review history for this task",
            "",
            "The following reviews were written for previous attempts. Read them carefully:",
            "- Check if issues flagged in prior reviews have been resolved",
            "- Do not re-flag issues that have already been addressed",
            "- Note any prior concerns that were NOT addressed and treat them as carry-forward requirements",
            "",
        ]
        for m in prior_review_msgs:
            attempt = m.get("attempt_number") or "?"
            review_history_lines.append(f"---\n[Attempt {attempt} Review]")
            review_history_lines.append(m.get("content", ""))
            review_history_lines.append("")
        prior_review_section = "\n".join(review_history_lines) + "\n"

    review_prompt = f"""# Code Review: {task['goal']}

{prior_review_section}## Component Context
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
# Branch Resolution + Auto-Merge (imported from switchboard.git.operations)
# ---------------------------------------------------------------------------


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
        import tempfile
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

    return await dispatch_task(
        project_id=task["project_id"],
        task_id=task_id,
        goal=task["goal"],
        phase="revisions" if review_feedback else "analysis",
        review_feedback=review_feedback,
    )


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
