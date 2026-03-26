"""switchboard.dispatch.gates — Test gate, review dispatch, and subtask orchestration.

Handles the full gate pipeline:
  - _run_subtask: lightweight CC session in parent's worktree
  - _run_test_gate: run project test_command, auto-retry on failure
  - _dispatch_review: build review prompt and run review subtask
  - _process_review_result_inline: check review outcome after inline subtask
  - _process_review_result: check review outcome from separate review task

Lazy imports from tasks (to break circular dependency):
  resume_task, retry_task, _check_and_dispatch_dependents, _update_usage
"""

import json
import logging
import os
import pwd
import shlex
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    UserMessage,
)
from claude_agent_sdk.types import TextBlock, ToolUseBlock

import switchboard.db as db
from switchboard.notifications import slack as notify
from switchboard.config.constants import (
    _DEFAULT_REVIEW_IGNORE_PATTERNS,
    _TAG_REVIEW_GUIDANCE,
    _DEFAULT_REVIEW_GUIDANCE,
)
from switchboard.config.settings import WORKER_USER
from switchboard.git.worktree import _run_as_worker
from switchboard.git.operations import _get_branch_diff, _filter_diff_by_ignore_patterns
from switchboard.dispatch.sdk_session import _open_shared

log = logging.getLogger("switchboard.tasks")


def _tail_lines(text: str, max_chars: int) -> str:
    """Truncate text to last ~max_chars, breaking at line boundaries."""
    if len(text) <= max_chars:
        return text
    cut = len(text) - max_chars
    idx = text.find("\n", cut)
    if idx == -1:
        return text[cut:]
    return text[idx + 1:]


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
    from tasks import _update_usage

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
    from tasks import resume_task, retry_task, _check_and_dispatch_dependents

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


async def _dispatch_review(task_id: str, project: dict, task: dict) -> None:
    """Run a lightweight review subtask in the parent's worktree."""
    from tasks import retry_task

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
    from tasks import retry_task, _check_and_dispatch_dependents

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
    from tasks import retry_task, _check_and_dispatch_dependents

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
