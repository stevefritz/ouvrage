"""ouvrage.dispatch.gates — Test gate, review dispatch, and subtask orchestration.

Handles the full gate pipeline:
  - _run_subtask: lightweight CC session in parent's worktree
  - _run_test_gate: run project test_command, auto-retry on failure
  - _dispatch_review: build review prompt and run review subtask
  - _process_review_result_inline: check review outcome after inline subtask
  - _process_review_result: check review outcome from separate review task

Lazy imports from ouvrage.dispatch.engine (to break circular dependency):
  _check_and_dispatch_dependents, _update_usage
Lazy imports from ouvrage.dispatch.lifecycle:
  lifecycle (for resume/retry via lifecycle.execute)
"""

import asyncio
import json
import logging
import os
import pwd
import shlex
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    UserMessage,
)
from claude_agent_sdk.types import TextBlock, ToolUseBlock

import ouvrage.db as db
from ouvrage.notifications import slack as notify
from ouvrage.config.settings import WORKER_USER, SKIP_CREDENTIAL_CHECK
from ouvrage.git.worktree import _run_as_worker
from ouvrage.dispatch.sdk_session import _open_shared
from ouvrage.dispatch._state import _running_gates, _gate_tasks

log = logging.getLogger(__name__)


def _tail_lines(text: str, max_chars: int) -> str:
    """Truncate text to last ~max_chars, breaking at line boundaries."""
    if len(text) <= max_chars:
        return text
    cut = len(text) - max_chars
    idx = text.find("\n", cut)
    if idx == -1:
        return text[cut:]
    return text[idx + 1:]


def _read_last_jsonl_timestamp(path: Path) -> datetime | None:
    """Return the timestamp of the last entry in a session JSONL file, or None."""
    try:
        if not path.exists():
            return None
        with open(path) as f:
            content = f.read()
        for line in reversed(content.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = entry.get("timestamp")
                if ts:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (json.JSONDecodeError, ValueError):
                continue
        return None
    except Exception:
        return None


# Prompt injected when resuming a stalled review subtask (strike 1 recovery).
# The reviewer already did the analysis — it just needs to complete output.
_REVIEW_RESUME_PROMPT = (
    "You were reviewing a task and your session was interrupted due to inactivity. "
    "Resume where you left off. If you have already completed your analysis, post "
    "your review result now using post_task_message with type='review' and title "
    "'APPROVED' or 'CHANGES REQUESTED'. If you haven't finished the analysis, "
    "continue reviewing and then post your result."
)


async def _run_test_streaming(worktree: str, test_command: str) -> tuple[str, int]:
    """Run test command with live output streaming to a log file.

    Tees stdout+stderr line by line to .ouvrage/test-output.log so the
    dashboard can poll it during execution.  Returns (full_output, returncode).
    """
    log_dir = Path(worktree) / ".ouvrage"
    log_dir.mkdir(parents=True, exist_ok=True)
    output_path = log_dir / "test-output.log"

    from ouvrage.git.worktree import _resolve_worker_identity
    identity = _resolve_worker_identity()
    env = os.environ.copy()

    # Note: preexec_fn is not safe with threads per Python docs, but asyncio's
    # subprocess implementation uses fork+exec on Linux where this runs in the
    # child process before exec. Safe for our single-threaded event loop use case.
    if identity is None:
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", f"cd {shlex.quote(worktree)} && {test_command}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    else:
        uid, gid, home = identity
        env["HOME"] = home

        def _demote():
            os.setgid(gid)
            os.setuid(uid)

        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", f"cd {shlex.quote(worktree)} && {test_command}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            preexec_fn=_demote,
            env=env,
        )

    chunks = []
    try:
        # Create file with explicit permissions before writing
        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w") as f:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                decoded = line.decode(errors="replace")
                chunks.append(decoded)
                f.write(decoded)
                f.flush()
    except Exception as e:
        log.warning(f"Error streaming test output: {e}")

    await proc.wait()
    return "".join(chunks), proc.returncode


async def _run_subtask(
    task_id: str,
    subtask_type: str,
    prompt: str,
    model: str = "opus",
    max_turns: int = 30,
    resume_session_id: str | None = None,
    inactivity_timeout: int | None = None,
) -> dict:
    """Run a lightweight CC session in the parent's worktree.

    No separate worktree, no setup_command, no gate pipeline.
    Returns the subtask record.

    resume_session_id: if set, resume this SDK session (strike 1 recovery).
    inactivity_timeout: if set, cancel if no JSONL activity for N seconds;
        returns subtask with status="stalled" and extra "_captured_session_id" key.
    """
    from ouvrage.dispatch.engine import _update_usage

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
    from ouvrage.git.worktree import _resolve_worker_identity
    _identity = _resolve_worker_identity()
    worker_home = _identity[2] if _identity else os.path.expanduser("~")
    # Point reviewer at /mcp/worker (trust-based, localhost-bypass) so tool
    # calls like post_task_message don't need OAuth. Matches the worker's
    # MCP endpoint in sdk_session.py.
    mcp_servers = {
        "ouvrage": {"type": "http", "url": f"http://localhost:{os.environ.get('OUVRAGE_PORT', '8100')}/mcp/worker"},
    }
    try:
        with open(os.path.join(worker_home, ".claude.json")) as f:
            for name, cfg in json.load(f).get("mcpServers", {}).items():
                if name not in mcp_servers:
                    mcp_servers[name] = cfg
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        pass

    log_dir = Path(worktree) / ".ouvrage"
    log_dir.mkdir(parents=True, exist_ok=True)
    stderr_path = log_dir / f"{subtask_type}-{count}-stderr.log"
    stderr_log = _open_shared(stderr_path)

    # Resolve Anthropic API key — skip if worker has CC subscription
    env = {"HOME": worker_home}
    _worker_has_oauth = False
    if SKIP_CREDENTIAL_CHECK:
        from ouvrage.git.worktree import _run_as_worker
        try:
            creds_path = str(Path(worker_home) / ".claude" / ".credentials.json")
            stdout, _, _ = await _run_as_worker(
                "python3", "-c",
                f"import json; d=json.load(open('{creds_path}')); "
                "o=d.get('claudeAiOauth',{}); print('yes' if o.get('accessToken') or o.get('refreshToken') else 'no')",
                cwd=worker_home,
            )
            _worker_has_oauth = (stdout.decode().strip() == "yes") if stdout else False
        except Exception:
            pass

    if not _worker_has_oauth:
        # Resolve which user's API key the proxy should decrypt.
        # The key itself is NOT passed to the worker — the proxy injects it.
        proxy_user_id = None
        task_record = await db.get_task(task_id)
        dispatched_by_id = task_record.get("dispatched_by") if task_record else None
        if dispatched_by_id:
            try:
                await db.get_anthropic_key(int(dispatched_by_id))
                proxy_user_id = int(dispatched_by_id)
            except (ValueError, TypeError):
                pass
        if proxy_user_id is None:
            try:
                instance = await db.get_instance()
                owner_id = instance.get("owner_user_id") if instance else None
                if owner_id:
                    await db.get_anthropic_key(int(owner_id))
                    proxy_user_id = int(owner_id)
            except (ValueError, TypeError):
                pass
        if proxy_user_id is not None:
            port = os.environ.get("OUVRAGE_PORT", "8100")
            env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}/proxy/anthropic/{proxy_user_id}"
            env["ANTHROPIC_AUTH_TOKEN"] = "proxy"  # CC checks for presence; proxy injects the real key

    options = ClaudeAgentOptions(
        user=WORKER_USER,
        cwd=str(worktree),
        env=env,
        permission_mode="bypassPermissions",
        model=model,
        max_turns=max_turns,
        setting_sources=["user", "project"],
        system_prompt={"type": "preset", "preset": "claude_code", "append": prompt if not resume_session_id else ""},
        mcp_servers=mcp_servers,
        disallowed_tools=["mcp__claude_ai_*"],
        debug_stderr=stderr_log,
        extra_args={"replay-user-messages": None},
    )

    if resume_session_id:
        options.resume = resume_session_id

    result_msg = None
    # Capture session_id from AssistantMessage for potential strike 1 resume.
    # Use a list for mutability in nested functions.
    _session_id_captured: list[str] = []
    log.info(f"Running subtask {subtask_id} (type={subtask_type}, model={model}, resume={resume_session_id is not None})")

    # Subtask session log — write to .ouvrage/{type}-{count}-session.jsonl
    subtask_log_path = log_dir / f"{subtask_type}-{count}-session.jsonl"
    subtask_log_file = _open_shared(subtask_log_path)

    def _log_subtask_msg(msg):
        entry = {"timestamp": db.now_iso(), "type": type(msg).__name__}
        try:
            if isinstance(msg, AssistantMessage):
                # Capture session_id for potential stall resume
                sid = getattr(msg, "session_id", None)
                if sid and not _session_id_captured:
                    _session_id_captured.append(sid)
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

    async def _sdk_loop():
        nonlocal result_msg
        initial_prompt = _REVIEW_RESUME_PROMPT if resume_session_id else prompt
        async with ClaudeSDKClient(options=options) as client:
            await client.query(initial_prompt)
            async for message in client.receive_response():
                _log_subtask_msg(message)
                if isinstance(message, ResultMessage):
                    result_msg = message

    stall_detected = False
    sdk_exception: Exception | None = None

    try:
        if inactivity_timeout:
            start_time = datetime.now(timezone.utc)
            # Check more frequently than the timeout, but at least every second
            # and at most every 30 seconds.
            check_interval = min(30, max(1, inactivity_timeout // 6))

            async def _watchdog():
                while True:
                    await asyncio.sleep(check_interval)
                    last_ts = _read_last_jsonl_timestamp(subtask_log_path)
                    ref = last_ts if last_ts is not None else start_time
                    idle = (datetime.now(timezone.utc) - ref).total_seconds()
                    if idle >= inactivity_timeout:
                        return  # signal stall by completing

            sdk_task = asyncio.create_task(_sdk_loop())
            watchdog_task = asyncio.create_task(_watchdog())
            done, _pending = await asyncio.wait(
                [sdk_task, watchdog_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            if watchdog_task in done:
                # Watchdog completed first — inactivity stall detected
                stall_detected = True
                sdk_task.cancel()
                try:
                    await sdk_task
                except (asyncio.CancelledError, Exception):
                    pass
            else:
                # SDK completed first
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
                if not sdk_task.cancelled():
                    exc = sdk_task.exception()
                    if exc:
                        sdk_exception = exc
        else:
            try:
                await _sdk_loop()
            except Exception as e:
                sdk_exception = e
    finally:
        stderr_log.close()
        subtask_log_file.close()

    if stall_detected:
        captured = _session_id_captured[0] if _session_id_captured else None
        log.warning(f"Subtask {subtask_id} stalled (inactivity={inactivity_timeout}s), captured_session_id={captured}")
        await db.update_subtask(subtask_id, status="stalled",
                                result="inactivity_stall", completed_at=db.now_iso())
        subtask = await db.get_subtask(subtask_id)
        # Attach captured session_id so caller can resume (strike 1)
        return dict(subtask, _captured_session_id=captured)

    if sdk_exception:
        log.exception(f"Subtask {subtask_id} error: {sdk_exception}")
        await db.update_subtask(subtask_id, status="failed",
                                result=str(sdk_exception), completed_at=db.now_iso())
        return await db.get_subtask(subtask_id)

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
    if task_id in _running_gates:
        log.warning(f"Gate already running for {task_id}, skipping duplicate")
        return
    _running_gates.add(task_id)
    _gate_tasks[task_id] = asyncio.current_task()
    try:
        await _run_test_gate_inner(task_id, project, task)
    finally:
        _running_gates.discard(task_id)
        _gate_tasks.pop(task_id, None)


async def _run_test_gate_inner(task_id: str, project: dict, task: dict) -> None:
    """Inner implementation of test gate (called by _run_test_gate after liveness check)."""
    from ouvrage.dispatch.lifecycle import lifecycle

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
    test_output, rc = await _run_test_streaming(worktree, test_command)

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
            await db.update_task(task_id, gate_status="passed")
        await db.write_audit_log(
            task_id=task_id, action="gate_passed",
            triggered_by="gate-pipeline",
            source_detail="_run_test_gate (tests passed)",
            previous_status=task.get("status"), new_status=task.get("status"),
        )
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
                        author="ouvrage",
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
                    await lifecycle.execute(task_id, "resume", triggered_by="gate",
                                            source_detail="dirty-worktree cleanup")
                    return  # Do not proceed to reviewer
            await _dispatch_review(task_id, project, task)
        else:
            # Tests passed, no review needed — complete via lifecycle
            await lifecycle.execute(task_id, "gate_pass",
                triggered_by="gate-pipeline",
                source_detail="_run_test_gate (tests passed, no review)",
            )
    else:
        # Refresh task to get current retry count
        task = await db.get_task(task_id)
        retries = (task.get("gate_retries") or 0) + 1
        max_retries = task.get("max_test_retries") or task.get("max_gate_retries") or 3
        await db.update_task(task_id, gate_status="test-failed", gate_retries=retries)
        await db.write_audit_log(
            task_id=task_id, action="gate_failed",
            triggered_by="gate-pipeline",
            source_detail=f"_run_test_gate (tests failed, attempt {retries}/{max_retries})",
            previous_status=task.get("status"), new_status=task.get("status"),
        )
        await db.post_task_message(
            task_id=task_id, author="dispatcher", type="test-result",
            title=f"Tests failed (attempt {retries}/{max_retries})",
            content=(
                f"```\n{_tail_lines(test_output, 3000)}\n```\n\n"
                f"Full output available at `.ouvrage/test-output.log` in your worktree, "
                f"or via `get_task_status(task_id='{task_id}', include_detail=true)`."
            ),
        )
        log.warning(f"Task {task_id}: test gate failed (attempt {retries}/{max_retries})")

        if retries < max_retries:
            # Auto-retry: dispatch new session with test failure as review feedback
            log.info(f"Task {task_id}: auto-retrying after test failure")
            await lifecycle.execute(task_id, "retry", triggered_by="gate",
                                    source_detail="test failure auto-retry",
                                    outcome="test_failure")
        else:
            await lifecycle.execute(task_id, "gate_fail",
                triggered_by="gate-pipeline",
                source_detail=f"_run_test_gate (tests failed {retries} times)",
                reason="max_test_retries",
            )


async def _dispatch_review(task_id: str, project: dict, task: dict) -> None:
    """Run a lightweight review subtask in the parent's worktree."""
    # Use gate_status as duplicate guard instead of _running_gates.
    # _running_gates can't be used here because _dispatch_review is called from
    # within _run_test_gate_inner, which still holds the task in _running_gates.
    # Checking gate_status=="reviewing" (set at the start of _dispatch_review_inner)
    # prevents true duplicates without blocking the normal test→review transition.
    fresh = await db.get_task(task_id)
    if fresh and fresh.get("gate_status") == "reviewing":
        log.warning(f"Review already in progress for {task_id}, skipping duplicate")
        return
    _running_gates.add(task_id)
    _gate_tasks[task_id] = asyncio.current_task()
    try:
        await _dispatch_review_inner(task_id, project, task)
    finally:
        _running_gates.discard(task_id)
        _gate_tasks.pop(task_id, None)


async def _dispatch_review_inner(task_id: str, project: dict, task: dict) -> None:
    """Inner implementation of review dispatch (called by _dispatch_review after liveness check)."""
    from ouvrage.dispatch.lifecycle import lifecycle
    await db.update_task(task_id, gate_status="reviewing")

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

    # --- Spec content ---
    pinned = await db.get_task_pinned(task_id)
    spec_content = pinned["content"] if pinned else "(no spec)"

    # --- Thread context (course corrections) ---
    thread = await db.read_task_messages(task_id)
    thread_msgs = thread.get("messages", [])
    human_msgs = [m for m in thread_msgs if m.get("author") not in ("dispatcher", "cc-worker")]
    course_corrections_section = ""
    if human_msgs:
        thread_lines = []
        for m in human_msgs:
            author = m.get("author", "user")
            title = m.get("title", "")
            content = m.get("content", "")
            thread_lines.append(f"**[{author}]** {(title + ': ') if title else ''}{content}")
        course_corrections_section = (
            "\n\n## Course Corrections / Notes from User\n"
            "The following messages were posted during development. "
            "These override the original spec where they conflict — treat them as authoritative.\n\n"
            + "\n".join(thread_lines)
            + "\n"
        )

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
            "## Prior Review History",
            "",
            "Do NOT re-flag resolved issues. Treat unresolved prior issues as carry-forward requirements.",
            "",
        ]
        for m in prior_review_msgs:
            attempt = m.get("attempt_number") or "?"
            review_history_lines.append(f"---\n[Attempt {attempt} Review]")
            review_history_lines.append(m.get("content", ""))
            review_history_lines.append("")
        prior_review_section = "\n".join(review_history_lines) + "\n"

    # --- Attempt-based leniency ---
    retry_leniency_section = ""
    if current_attempt > 1:
        retry_leniency_section = (
            "\nThis is a retry. Prior attempts already consumed resources.\n"
            "Only request changes for: bugs, unmet spec requirements, security issues, missing tests.\n"
            "Do NOT reject for style, naming, or cosmetic issues on retries.\n"
        )

    base_branch = task.get("base_branch") or project.get("default_branch")
    if not base_branch:
        raise ValueError(f"Task {task_id}: no base_branch on task and no default_branch on project — cannot build review diff")
    worktree_path = task.get("worktree_path") or "(unknown)"
    test_command = project.get("test_command") or "(none configured)"

    # Fetch origin so the reviewer diffs against the current remote main,
    # not a stale local ref. Errors are logged but non-fatal.
    if worktree_path and worktree_path != "(unknown)":
        try:
            _stdout, _stderr, _rc = await _run_as_worker(
                "git", "-C", worktree_path, "fetch", "origin", base_branch
            )
            if _rc != 0:
                log.warning(
                    f"Task {task_id}: git fetch origin {base_branch} returned rc={_rc}: "
                    f"{_stderr.decode(errors='replace').strip()}"
                )
        except Exception as e:
            log.warning(f"Task {task_id}: git fetch origin {base_branch} failed: {e}")

    review_prompt = f"""# You are an Ouvrage code reviewer

You were dispatched to review task `{task_id}` on project `{task.get('project_id')}`.
Branch: `{task.get('branch')}` | Base: `{base_branch}` | Worktree: `{worktree_path}`
This is attempt **{current_attempt}** of this task.

## Task Lifecycle — Your Place In It

1. User dispatches a task with a spec and checklist
2. CC worker implements in an isolated worktree — commits, pushes
3. **Test gate** runs `{test_command}` — tests passed (exit code 0) or you would not be running
4. **Review gate (you)** — you evaluate the work against the spec
5. If you approve → PR created or branch merged, dependent tasks dispatch
6. If you request changes → worker is retried with your feedback as revision instructions — this costs real time and money

You are the final gate before code ships.

{prior_review_section}## Task Spec

**Goal:** {task.get('goal')}

{spec_content}
{course_corrections_section}
## Component Context
{component_section}

## Punchlist Items Claimed
{punchlist_section}

## Reviewing the Changes

You have full filesystem access to the worktree at `{worktree_path}`.

1. Run `git diff origin/{base_branch}...HEAD` to see all changes
2. Read the diff carefully — if it's large, review file by file: `git diff origin/{base_branch}...HEAD -- path/to/file`
3. When you need context beyond the diff, read the full file
4. Check test files alongside implementation files
5. If the task added images or non-text files, verify they exist: `ls -la path/`

Do NOT rely on a pre-built diff. Investigate the code yourself.

Ignore in review: lockfiles, .ouvrage/ artifacts, auto-generated files.

## Review Criteria — Priority Order

**1. Spec compliance (critical)**
Does the code fulfill every requirement in the spec? Are all checklist items addressed?
If course corrections exist, they override the original spec where they conflict.

**2. Correctness (critical)**
Logic bugs, off-by-one errors, race conditions, unhandled edge cases, error paths.
Security: injection, XSS, auth bypass, secret exposure.

**3. Test quality (important)**
Tests must assert spec requirements, not mirror code output.
Failure paths tested, not just happy paths.
New functionality must have corresponding tests.

**4. Punchlist verification (if applicable)**
Claimed punchlist items must be actually addressed — not partial or superficial fixes.

**5. Code quality (advisory only)**
Naming, complexity, dead code. Do NOT request changes solely for style.

## Severity Calibration
{retry_leniency_section}
**Request changes when:**
- Spec requirements are unmet
- Logic bugs that cause incorrect behavior
- Security vulnerabilities
- Missing tests for new functionality
- Incorrect test assertions
- Claimed punchlist items not actually fixed

**Approve when:**
- All spec requirements met
- Code correct, reasonable edge cases handled
- Tests exist and test the right things
- No security issues
- Minor style nits acceptable — note them but approve

**Approve with notes:**
Title="APPROVED", include suggestions in body. Visible but won't trigger retry.

## Writing Feedback

When requesting changes, your feedback becomes the worker's revision instructions.

**DO:**
- Reference specific files and line numbers
- State what's wrong AND what the fix should be
- Mark blockers vs suggestions: "**BLOCKER:** ..." / "**SUGGESTION:** ..."
- Group related issues

**DON'T:**
- Say "needs improvement" without specifics
- Re-flag issues resolved in prior attempts
- Request changes to code not modified in this task
- Suggest features beyond spec scope
- Request stylistic changes that don't affect correctness

## Output

Post exactly one review message:

For approval:
- title="APPROVED"
- type="review"
- Content: brief summary of what passes. Optional non-blocking notes.

For changes requested:
- title="CHANGES REQUESTED"
- type="review"
- Content:
  ### Blockers
  1. **[file:line]** Issue → expected fix
  2. **[file:line]** Issue → expected fix

  ### Suggestions (non-blocking)
  - Optional improvements

The title field is the gate signal. Use exactly "APPROVED" or "CHANGES REQUESTED". Nothing else.

Post your review:
mcp__ouvrage__post_task_message(task_id='{task_id}', author='cc-worker', type='review', title='APPROVED' or 'CHANGES REQUESTED', content='...')
"""

    from ouvrage.config.constants import REVIEW_INACTIVITY_TIMEOUT_SECONDS

    review_model = task.get("review_model") or "opus"
    log.info(f"Running subtask review for {task_id}")
    try:
        subtask = await _run_subtask(
            task_id=task_id,
            subtask_type="review",
            prompt=review_prompt,
            model=review_model,
            inactivity_timeout=REVIEW_INACTIVITY_TIMEOUT_SECONDS,
        )

        # ── Stall handling: strike 1 (resume) and strike 2 (halt) ──────────
        if subtask.get("status") == "stalled":
            captured_session_id = subtask.get("_captured_session_id")
            timeout_min = REVIEW_INACTIVITY_TIMEOUT_SECONDS // 60
            log.warning(f"Review subtask stalled (strike 1) for {task_id}, session_id={captured_session_id}")
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title=f"Review stalled — resuming (strike 1/2)",
                content=(
                    f"Review session was inactive for {timeout_min} minutes. "
                    f"Resuming the same session to recover the in-progress analysis."
                ),
            )
            # Strike 1: resume the same session
            subtask2 = await _run_subtask(
                task_id=task_id,
                subtask_type="review",
                prompt=review_prompt,
                model=review_model,
                resume_session_id=captured_session_id,
                inactivity_timeout=REVIEW_INACTIVITY_TIMEOUT_SECONDS,
            )
            if subtask2.get("status") == "stalled":
                # Strike 2: halt, require manual intervention
                log.error(f"Review subtask stalled twice (strike 2) for {task_id} — halting")
                await db.post_task_message(
                    task_id=task_id, author="dispatcher", type="status",
                    title="Review stalled twice — manual retry required (strike 2/2)",
                    content=(
                        f"Review session stalled again after {timeout_min} minutes of inactivity. "
                        f"Setting gate_status=needs-review. "
                        f"Use retry_task to re-run the gate pipeline when ready."
                    ),
                )
                await db.update_task(task_id, gate_status="needs-review")
                await notify.task_needs_review(
                    task_id=task_id,
                    reason=f"Review stalled twice (>{timeout_min}m inactivity each time). Manual retry needed.",
                )
                return
            # Use subtask2 for further processing
            subtask = subtask2

        if subtask.get("status") == "completed":
            await _process_review_result_inline(task_id)
        else:
            log.warning(f"Review subtask failed for {task_id}: {subtask.get('error', 'unknown')}")
            task = await db.get_task(task_id)
            retries = (task.get("gate_retries") or 0) + 1
            max_retries = task.get("max_review_retries") or task.get("max_gate_retries") or 3
            await db.update_task(task_id, gate_status="review-failed", gate_retries=retries)
            await db.post_task_message(
                task_id=task_id, author="dispatcher", type="status",
                title="Review failed",
                content=f"Review subtask did not complete (attempt {retries}/{max_retries}).\n\n"
                        f"Error: {subtask.get('error', 'process killed or crashed')}",
            )
            if retries < max_retries:
                log.info(f"Retrying review for {task_id} (attempt {retries + 1})")
                await _dispatch_review_inner(task_id, await db.get_project(task["project_id"]), task)
            else:
                await lifecycle.execute(task_id, "gate_fail",
                    triggered_by="gate-pipeline",
                    source_detail="Review subtask failed after max retries",
                    reason="max_review_retries",
                )
    except Exception as e:
        log.error(f"Failed to run review subtask for {task_id}: {e}")
        task = await db.get_task(task_id)
        retries = (task.get("gate_retries") or 0) + 1
        max_retries = task.get("max_review_retries") or task.get("max_gate_retries") or 3
        await db.update_task(task_id, gate_status="review-failed", gate_retries=retries)
        if retries < max_retries:
            try:
                await _dispatch_review_inner(task_id, await db.get_project(task["project_id"]), task)
            except Exception:
                log.exception(f"Review retry also failed for {task_id}")
                await lifecycle.execute(task_id, "gate_fail",
                    triggered_by="gate-pipeline",
                    source_detail="Review retry also failed",
                    reason="max_review_retries",
                )
        else:
            await lifecycle.execute(task_id, "gate_fail",
                triggered_by="gate-pipeline",
                source_detail=f"Review failed: {e}",
                reason="max_review_retries",
            )


async def _process_review_result_inline(task_id: str) -> None:
    """Check review messages on task and process approval/rejection."""
    from ouvrage.dispatch.lifecycle import lifecycle

    msgs = await db.read_task_messages(task_id)
    review_msg = next(
        (m for m in reversed(msgs.get("messages", []))
         if m.get("type") == "review"),
        None,
    )

    if review_msg and (review_msg.get("title") or "").strip().upper() == "APPROVED":
        log.info(f"Review approved for {task_id}")
        await lifecycle.execute(task_id, "gate_pass",
            triggered_by="gate-pipeline",
            source_detail="_process_review_result_inline (review approved)",
        )
    else:
        task = await db.get_task(task_id)
        retries = (task.get("gate_retries") or 0) + 1
        max_retries = task.get("max_review_retries") or task.get("max_gate_retries") or 3
        await db.update_task(task_id, gate_status="review-failed", gate_retries=retries)
        await db.write_audit_log(
            task_id=task_id, action="gate_failed",
            triggered_by="gate-pipeline",
            source_detail=f"_process_review_result_inline (review failed, attempt {retries}/{max_retries})",
            previous_status=task.get("status"), new_status=task.get("status"),
        )
        log.warning(f"Review failed for {task_id} (attempt {retries}/{max_retries})")

        if retries < max_retries:
            await lifecycle.execute(task_id, "retry", triggered_by="review",
                                    source_detail="review rejection auto-retry (inline)",
                                    outcome="review_rejected")
        else:
            await lifecycle.execute(task_id, "gate_fail",
                triggered_by="gate-pipeline",
                source_detail=f"_process_review_result_inline (review failed {retries} times)",
                reason="max_review_retries",
            )


async def _process_review_result(review_task_id: str, parent_task_id: str) -> None:
    """Check if review approved or requested changes."""
    from ouvrage.dispatch.lifecycle import lifecycle

    msgs = await db.read_task_messages(parent_task_id)
    review_msg = next(
        (m for m in reversed(msgs.get("messages", []))
         if m.get("type") == "review"),
        None,
    )

    if review_msg and (review_msg.get("title") or "").strip().upper() == "APPROVED":
        log.info(f"Review approved for {parent_task_id}")
        await lifecycle.execute(parent_task_id, "gate_pass",
            triggered_by="gate-pipeline",
            source_detail="_process_review_result (review approved)",
        )
    else:
        parent = await db.get_task(parent_task_id)
        retries = (parent.get("gate_retries") or 0) + 1
        max_retries = parent.get("max_review_retries") or parent.get("max_gate_retries") or 3
        await db.update_task(parent_task_id, gate_status="review-failed", gate_retries=retries)
        await db.write_audit_log(
            task_id=parent_task_id, action="gate_failed",
            triggered_by="gate-pipeline",
            source_detail=f"_process_review_result (review failed, attempt {retries}/{max_retries})",
            previous_status=parent.get("status"), new_status=parent.get("status"),
        )
        log.warning(f"Review failed for {parent_task_id} (attempt {retries}/{max_retries})")

        if retries < max_retries:
            await lifecycle.execute(parent_task_id, "retry", triggered_by="review",
                                    source_detail="review rejection auto-retry",
                                    outcome="review_rejected")
        else:
            await lifecycle.execute(parent_task_id, "gate_fail",
                triggered_by="gate-pipeline",
                source_detail=f"_process_review_result (review failed {retries} times)",
                reason="max_review_retries",
            )


async def _resume_gate_pipeline(task_id: str, reason: str = "recovery") -> bool | None:
    """Unified gate recovery — single entry point for recovering any interrupted gate state.

    Returns True if the gate was interrupted and recovery was handled (gate re-entered).
    Returns False if the gate state is a normal rejection (test-failed, review-failed,
    needs-review) — the caller must launch a new CC session with feedback instead.
    Returns None if the task or project was not found.

    Reads fresh state from DB at invocation time to avoid race conditions.
    Re-checks _running_gates before taking action to prevent races with normal completion.

    Does NOT reset gate_retries when recovering interrupted states (testing/reviewing).
    For test-failed/review-failed, resets gate_status=None before calling retry_task so
    retry_task's gate re-entry check doesn't recurse back here.
    For needs-review, resets gate_retries=0 (same as original retry_task re-entry behavior).
    """
    # Read fresh state from DB
    task = await db.get_task(task_id)
    if not task:
        log.warning(f"_resume_gate_pipeline: task {task_id} not found")
        return None

    project = await db.get_project(task["project_id"])
    if not project:
        log.warning(f"_resume_gate_pipeline: project not found for {task_id}")
        return None

    # Re-check _running_gates to prevent race with normal completion
    if task_id in _running_gates:
        log.info(f"_resume_gate_pipeline: gate already running for {task_id}, skipping ({reason})")
        return await db.get_task(task_id)

    # Guard: verify worktree exists before re-entering any gate path.
    # If the worktree was released (e.g. task completed + auto-release), the gate
    # cannot run. Set needs-review so a human can decide what to do.
    worktree = task.get("worktree_path")
    if not worktree or not os.path.exists(worktree):
        log.warning(f"_resume_gate_pipeline: worktree missing for {task_id}, setting needs-review ({reason})")
        await db.update_task(task_id, gate_status="needs-review")
        await db.post_task_message(
            task_id=task_id, author="ouvrage", type="status",
            title="Gate recovery blocked — worktree missing",
            content=f"Cannot resume gate pipeline: worktree not found. Reason: {reason}.",
        )
        return False

    gate = task.get("gate_status")
    gate_retries = task.get("gate_retries") or 0
    max_test_retries = task.get("max_test_retries") or task.get("max_gate_retries") or 3
    max_review_retries = task.get("max_review_retries") or task.get("max_gate_retries") or 3

    log.info(f"_resume_gate_pipeline: {task_id} gate={gate} retries={gate_retries} reason={reason}")

    await db.post_task_message(
        task_id=task_id, author="ouvrage", type="status",
        title=f"Gate recovery ({reason})",
        content=f"Recovering task from gate state `{gate or 'None'}` (reason: {reason}).",
    )

    if gate is None:
        # Gate pipeline never started — check if push is needed first
        if not task.get("pushed_at"):
            from ouvrage.git.operations import _ensure_branch_pushed
            pushed = await _ensure_branch_pushed(task_id, task)
            if not pushed:
                await db.update_task(task_id, gate_status="push-failed")
                return True  # Handled (set push-failed state)
            await db.update_task(task_id, pushed_at=db.now_iso())
            task = await db.get_task(task_id)
        # Enter gate pipeline from top
        asyncio.create_task(_run_test_gate(task_id, project, task))
        return True  # Gate interrupted before starting — re-entering pipeline

    elif gate == "testing":
        # Gate was running when server died — re-run (tests are idempotent)
        asyncio.create_task(_run_test_gate(task_id, project, task))
        return True  # Gate was interrupted mid-test — re-running

    elif gate == "test-failed":
        # Normal rejection — code needs fixing. DO NOT re-run gates.
        # Return False so caller (recovery or retry_task) can launch CC with failure feedback.
        if gate_retries >= max_test_retries:
            await db.update_task(task_id, gate_status="needs-review")
            await notify.task_needs_review(
                task_id=task_id,
                reason=f"Tests failed {gate_retries} times. Manual intervention needed.",
            )
        return False  # Caller should launch CC retry (not gate retry)

    elif gate == "test-passed":
        # Tests passed but review never started
        if task.get("auto_review"):
            asyncio.create_task(_dispatch_review(task_id, project, task))
        else:
            await db.update_task(task_id, gate_status="passed", gate_passed_at=db.now_iso())
            from ouvrage.dispatch.engine import _check_and_dispatch_dependents
            await _check_and_dispatch_dependents(task_id)
        return True  # Gate was interrupted between test-pass and review — re-entering

    elif gate == "reviewing":
        # Server died during review — start fresh (don't try to resume dead session).
        # Reset gate_status to test-passed so _dispatch_review's duplicate guard
        # (which checks gate_status == "reviewing") doesn't block recovery.
        await db.update_task(task_id, gate_status="test-passed")
        task = await db.get_task(task_id)
        asyncio.create_task(_dispatch_review(task_id, project, task))
        return True  # Gate was interrupted mid-review — re-dispatching

    elif gate == "review-failed":
        # Normal rejection — code needs fixing. DO NOT re-run gates.
        # Return False so caller (recovery or retry_task) can launch CC with review feedback.
        if gate_retries >= max_review_retries:
            await db.update_task(task_id, gate_status="needs-review")
            await notify.task_needs_review(
                task_id=task_id,
                reason=f"Review failed {gate_retries} times. Manual intervention needed.",
            )
        return False  # Caller should launch CC retry (not gate retry)

    elif gate == "needs-review":
        # Terminal state — user must decide. DO NOT re-run gates.
        # Code was rejected and max retries exceeded (or review stalled twice).
        # A new CC session needs the user's explicit direction.
        return False  # Caller should not auto-retry; user intervention required

    elif gate == "push-failed":
        # Re-attempt push — if it succeeds, reset gate_status and re-enter pipeline
        from ouvrage.git.operations import _ensure_branch_pushed
        pushed = await _ensure_branch_pushed(task_id, task)
        if pushed:
            await db.update_task(task_id, gate_status=None, pushed_at=db.now_iso())
            task = await db.get_task(task_id)
            asyncio.create_task(_run_test_gate(task_id, project, task))
        else:
            log.info(f"_resume_gate_pipeline: {task_id} push still failing — leaving as push-failed (user must fix PAT)")
        return True  # Handled (either re-entered pipeline or left in push-failed)

    elif gate == "passed":
        # Edge case: passed but gate_passed_at not set
        if not task.get("gate_passed_at"):
            from ouvrage.dispatch.engine import _check_and_dispatch_dependents
            await _check_and_dispatch_dependents(task_id)
        return True  # Handled

    else:
        log.warning(f"_resume_gate_pipeline: unknown gate_status={gate!r} for {task_id}")
        return False
