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
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    UserMessage,
)
from claude_agent_sdk.types import TextBlock, ToolUseBlock, ToolResultBlock

import database as db
import notifications as notify

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


async def recover_orphaned_tasks():
    """Recover tasks left in broken states after a service restart.

    Scans for:
    - 'working' tasks with no live PID → mark as needs-review
    - 'test-failed' or 'review-failed' gate status → re-trigger gate pipeline
    """
    tasks = await db.list_tasks(status="working")
    for task in tasks:
        pid = task.get("pid")
        if pid and _is_pid_alive(pid):
            continue  # still running, leave it alone
        log.warning(f"Startup recovery: task {task['id']} stuck in 'working' with no live process")
        await db.update_task(task["id"], status="needs-review")
        await db.post_task_message(
            task_id=task["id"], author="dispatcher", type="status",
            title="Recovered after restart",
            content="Service restarted while this task was running. Marked as needs-review. "
                    "Resume or retry to continue.",
        )

    # Re-trigger gate for tasks stuck in test-failed/review-failed
    all_tasks = await db.list_tasks(status="completed")
    for task in all_tasks:
        gate = task.get("gate_status")
        if gate in ("test-failed", "review-failed"):
            log.warning(f"Startup recovery: task {task['id']} has gate_status={gate}, re-triggering gate")
            project = await db.get_project(task["project_id"])
            if not project:
                continue
            if gate == "test-failed" and task.get("auto_test") and project.get("test_command"):
                asyncio.create_task(_run_test_gate(task["id"], project, task))
            elif gate == "review-failed" and task.get("auto_review"):
                asyncio.create_task(_dispatch_review(task["id"], project, task))


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

WORKER_USER = "switchboard"


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
            "url": "http://localhost:8100/mcp",
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
        user="switchboard",
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
                    # Gate already passed previously — this is a manual resume cycle
                    # Don't re-run gate or auto-advance chain
                    log.info(f"Task {task_id}: gate already passed, skipping gate pipeline (manual resume)")
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
    mcp_servers = {"switchboard": {"type": "http", "url": "http://localhost:8100/mcp"}}
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
        user="switchboard",
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


async def _dispatch_review(task_id: str, project: dict, task: dict) -> None:
    """Run a lightweight review subtask in the parent's worktree."""
    await db.update_task(task_id, gate_status="reviewing")

    diff_output = await _get_branch_diff(task)

    # Get spec content
    pinned = await db.get_task_pinned(task_id)
    spec_content = pinned["content"] if pinned else "(no spec)"

    # Include message thread so reviewer sees course corrections
    thread = await db.read_task_messages(task_id)
    thread_msgs = thread.get("messages", [])
    # Filter to human-authored messages (notes, review feedback, answers) — skip dispatcher status
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

## Original Spec
{spec_content}
{thread_context}
## Changes to Review
```
{diff_output[:10000]}
```

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
            log.warning(f"Review subtask failed for {task_id}, falling back to gate pass")
            await db.update_task(task_id, gate_status="passed", gate_passed_at=db.now_iso())
            await _check_and_dispatch_dependents(task_id)
    except Exception as e:
        log.error(f"Failed to run review subtask for {task_id}: {e}")
        await db.update_task(task_id, gate_status="passed", gate_passed_at=db.now_iso())
        await _check_and_dispatch_dependents(task_id)


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
    """If any tasks depend on this one and it's gate-passed, dispatch them."""
    task = await db.get_task(task_id)
    if not task or not task.get("gate_passed_at"):
        return

    dependents = await db.get_dependents(task_id)
    dispatched_any = False
    for dep in dependents:
        if dep["status"] == "ready":
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
            jira_ticket=jira_ticket, conversation_id=conversation_id,
            model=model, auto_test=auto_test, depends_on=depends_on,
            auto_review=auto_review, review_model=review_model,
            parent_task_id=parent_task_id, auto_pr=auto_pr,
            component_id=component_id,
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
                }
    elif task["status"] in ("needs-review", "turns-exhausted", "completed"):
        is_resume = True
        # Update depends_on if caller provided a new value (fixes stale prefix issue)
        if depends_on and task.get("depends_on") != depends_on:
            await db.update_task(task_id, depends_on=depends_on)
            task["depends_on"] = depends_on
    elif task["status"] == "working":
        raise RuntimeError(f"Task '{task_id}' is already running")

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
    }


async def resume_task(task_id: str) -> dict:
    """Resume a paused task with the same session ID."""
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")
    if task["status"] not in ("needs-review", "turns-exhausted", "completed"):
        raise ValueError(f"Task '{task_id}' is in status '{task['status']}', expected 'needs-review', 'turns-exhausted', or 'completed'")

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

    # Clear session and gate state to force fresh run through the pipeline
    await db.update_task(task_id, session_id=None, gate_status=None, gate_passed_at=None)

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
