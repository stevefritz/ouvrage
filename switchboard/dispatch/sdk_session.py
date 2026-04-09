"""switchboard.dispatch.sdk_session — Claude Agent SDK session management.

Handles everything needed to run a CC worker session:
  - Prompt building (_build_task_prompt, _build_resume_prompt)
  - Log directory setup and shared-file utilities
  - The main SDK dispatch loop (_run_sdk_session)
  - Result logging (_log_result)

Also applies the anyio process isolation patch at module import time so that
all CC subprocess spawns get their own session/process group.

Lazy imports from dispatch siblings (to break circular dependency):
  gates: _run_test_gate, _dispatch_review, _process_review_result
  engine: _check_and_dispatch_dependents, _update_usage
  queue: _drain_queue
  _state: _active_clients
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
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    UserMessage,
)
from claude_agent_sdk.types import TextBlock, ToolPermissionContext, ToolUseBlock, ToolResultBlock

import switchboard.db as db
from switchboard.notifications import slack as notify
from switchboard.config.settings import WORKER_USER, SKIP_CREDENTIAL_CHECK
from switchboard.config.constants import MESSAGE_POLL_INTERVAL, DEFAULT_MODEL
from switchboard.git.worktree import _run_as_worker

log = logging.getLogger(__name__)

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
# Both this module and the SDK transport module reference the same anyio module
# object, so patching anyio.open_process here affects all SDK subprocess spawns.
_orig_anyio_open_process = anyio.open_process


async def _isolated_open_process(command, *, start_new_session: bool = False, **kwargs):
    """Wrapper that forces start_new_session=True for all subprocess spawns."""
    return await _orig_anyio_open_process(command, start_new_session=True, **kwargs)


anyio.open_process = _isolated_open_process


# ---------------------------------------------------------------------------
# Prompt Building
# ---------------------------------------------------------------------------

def _human_size_prompt(size_bytes: int) -> str:
    """Format byte count as human-readable string for prompt injection."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes}B"


async def _build_task_prompt(project: dict, task: dict, spec_content: str | None,
                             checklist: list[dict] | None = None,
                             escalation_criteria: str | None = None,
                             review_feedback: list[dict] | None = None) -> str:
    """Build the prompt CC receives when dispatched."""
    parts = []

    # ── 1. Revision header ──────────────────────────────────────────────────
    if review_feedback:
        current_attempt = task.get("current_attempt", 1)
        parts.append("# ⚠️ REVISION REQUESTED")
        parts.append("")
        parts.append("This task was previously completed but needs revisions based on review feedback.")
        parts.append("**Your primary job is to address the feedback below.** The original spec is included")
        parts.append("for context, but focus on the reviewer's requested changes.")
        parts.append(f"This is attempt {current_attempt}.")
        parts.append("")
        parts.append("## Review Feedback")
        for msg in review_feedback:
            author = msg.get("author", "reviewer")
            title = msg.get("title", "")
            header = f"### {title}" if title else f"### From {author}"
            parts.append(header)
            parts.append(msg.get("content", ""))
            parts.append("")

    # ── 2. Prior task context (dependency chain) ────────────────────────────
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

    # ── 3. Identity & environment ────────────────────────────────────────────
    dispatched_by = task.get("dispatched_by") or "system"
    worktree_path = task.get("worktree_path") or "(unknown)"
    branch = task["branch"]
    task_id = task["id"]
    project_id = project["id"]

    parts.append("# You are an Ouvrage worker")
    parts.append("")
    parts.append(f"Dispatched by **{dispatched_by}** for project **{project_id}**.")
    parts.append(f"Branch: `{branch}` | Worktree: `{worktree_path}` | Task ID: `{task_id}`")
    parts.append("")
    parts.append("You are a headless remote worker. The user is not watching your terminal.")
    parts.append("They see your work through the dashboard: phase, checklist, and posted messages.")
    parts.append("If you don't post updates, your task looks dead. Be proactively communicative.")
    parts.append("You may be one of several workers running in parallel across different tasks.")
    parts.append(f"Your branch is `{branch}`. That is the only branch you touch.")
    parts.append("")

    # ── 4. Task header + spec + checklist ────────────────────────────────────
    parts.append(f"# Task: {task['goal']}")
    parts.append(f"Project: {project_id} | Branch: {branch}")
    parts.append(f"Task ID: {task_id}")
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

    # ── 5. Grounding phase (skip for revision retries) ──────────────────────
    if not review_feedback:
        parts.append("## Grounding Phase — MANDATORY")
        parts.append("")
        parts.append("Do this BEFORE writing any code:")
        parts.append("")
        parts.append("1. Read the relevant source files for this task. Understand WHY this is being requested, not just WHAT.")
        parts.append("2. Validate the checklist against the actual code. Adjust items: fix inaccuracies, add missing ones, remove irrelevant ones. If the approach fundamentally won't work or scope is significantly larger than expected, set phase to `needs-review` and explain.")
        parts.append(f"3. Post your **Implementation Plan** as a `plan` message. Title it exactly 'Implementation Plan'.")
        parts.append("")
        parts.append("The plan must be detailed enough for a lead to review your approach and correct you if needed. Include:")
        parts.append("- Which files you will modify and what changes you'll make in each")
        parts.append("- Any new files you'll create")
        parts.append("- Your testing approach")
        parts.append("- Any assumptions or risks")
        parts.append("")
        parts.append("**Example plan:**")
        parts.append("")
        parts.append("```")
        parts.append("## Implementation Plan")
        parts.append("")
        parts.append("Files to modify:")
        parts.append("1. `switchboard/server/handlers/tasks.py` — Add `files` array to the task status response.")
        parts.append("   Query `db.list_files(task_id)` in `_handle_get_task_status` and include compact file metadata.")
        parts.append("2. `switchboard/server/tools.py` — Register `get_file` tool on user endpoint alongside existing `get_attached_file`.")
        parts.append("   Point both to the same handler in `files_handler.py`.")
        parts.append("")
        parts.append("Assumptions:")
        parts.append("- `list_files` already supports filtering by task_id (verified in db/files.py)")
        parts.append("- No schema migration needed — files table already has all required columns")
        parts.append("```")
        parts.append("")
        parts.append("**Do not begin coding until the plan is posted.**")
        parts.append("")

    # ── 6. Reference files ───────────────────────────────────────────────────
    task_files = await db.list_files(task_id=task["id"])
    if task_files:
        parts.append("## Reference Files")
        parts.append("The following files were uploaded for this task:")
        for f in task_files:
            size_bytes = f.get("size_bytes") or 0
            human_size = _human_size_prompt(size_bytes)
            parts.append(f"- {f['stored_path']} ({f.get('mime_type', 'unknown')}, {human_size})")
        parts.append("")
        parts.append("Read these files when relevant to your task.")
        parts.append("")

    # ── 7. Producing Files ───────────────────────────────────────────────────
    parts.append("## Producing Files")
    parts.append(
        "If your task produces files the user should see (reports, screenshots, analyses, exports), "
        "use the add_task_file tool to persist them. Pass the absolute file path within your worktree."
    )
    parts.append(
        f"  `mcp__switchboard__add_task_file(task_id='{task_id}', source_path='/absolute/path/in/worktree/file.pdf')`"
    )
    parts.append("The file will be saved permanently and appear in the task's Files section for download.")
    parts.append("")

    # ── 8. How to work ─────────────────────────────────────────────────────────
    parts.append("## How to Work")
    parts.append("")
    parts.append("- Post a `progress` message every time you complete a major step — the user watches the dashboard, not your terminal.")
    parts.append("- Update your phase as you transition: grounding → implementing → testing → finishing.")
    parts.append("- Mark checklist items done as you go, not all at once at the end.")
    parts.append("- Use `add_task_file` for any deliverable — reports, docs, analysis outputs.")
    parts.append("- Post a `question` if stuck — it pauses your session and notifies the user. Don't guess.")
    parts.append("")
    parts.append("Key tools: `post_task_message` (progress/question/result/handoff/plan), "
                 "`update_task_phase`, `update_task_checklist`, `add_checklist_item`, `add_task_file`, "
                 "`git_push`, `git_fetch`.")
    parts.append("")
    parts.append(f"- Update checklist: `mcp__switchboard__update_task_checklist(item_id=<id>, done=true)`")
    parts.append(f"- Update phase: `mcp__switchboard__update_task_phase(task_id='{task_id}', phase='implementing', detail='...')`")
    parts.append(f"- Post progress: `mcp__switchboard__post_task_message(task_id='{task_id}', author='cc-worker', type='progress', content='...')`")
    parts.append("")

    # ── 9. Search & context ──────────────────────────────────────────────────
    parts.append("## Finding Context")
    parts.append("")
    parts.append(f"You have access to project memory via `search(query, project_id='{project_id}')`. "
                 "It returns ranked pointers with snippets and entity IDs.")
    parts.append("Follow up with `read(around=entity_id)` for full context around a match.")
    parts.append("If the spec is clear, start working. If you need context the spec doesn't provide, "
                 "search for it. One or two targeted searches, not a research project.")
    parts.append("You can also read project conversations for design decisions — "
                 "use `read_task_messages(task_id)` for chain context from parent tasks.")
    parts.append("")

    # ── 10. Safety ────────────────────────────────────────────────────────────
    parts.append("## Safety")
    parts.append("")
    parts.append("**System integrity:**")
    parts.append("- Don't modify Ouvrage system files, MCP server code, or infrastructure configs.")
    parts.append("- Don't read or expose API keys, credentials, `.env` files, or encryption keys in messages or commits.")
    parts.append("- Don't access other worktrees or other tasks' files outside your own worktree.")
    parts.append("")
    parts.append("**Git safety:**")
    parts.append("- Do not run `git push` or `git fetch` directly — these are blocked. Use the MCP tools "
                 f"`git_push(task_id='{task_id}')` and `git_fetch(task_id='{task_id}', ref=...)` instead. "
                 "The platform handles authentication. All local git operations (commit, merge, diff, log, status, add, checkout) work normally.")
    parts.append(f"- Never `git push --force`, `--force-with-lease`, or `-f`. If push fails, push to a rescue branch and post a question.")
    parts.append("- Never `git rebase`. Use `git merge origin/main` if you need upstream changes.")
    parts.append("- Never `git remote add` or modify remotes. Never create tags.")
    parts.append(f"- Never checkout, merge to, or push to branches other than `{branch}`.")
    parts.append("- Never modify `.gitignore`, CI/CD configs (`.github/workflows/`), or deploy files unless the spec explicitly requires it.")
    parts.append("")
    parts.append("**Process safety:**")
    parts.append("- Never `kill`/`pkill`/`killall` (mechanically blocked by hook).")
    parts.append("- Always wrap test runs and long commands in `timeout`.")
    parts.append("- Don't run the full test suite — run targeted tests, the gate handles the full suite.")
    parts.append("")
    parts.append("**Scope safety:**")
    parts.append("- Your scope is the spec and checklist. Nothing more.")
    parts.append("- Don't refactor code outside your task scope. Don't install new frameworks unless the spec requires it.")
    parts.append("- If scope is bigger than expected, escalate — don't silently expand.")
    parts.append("- If you find a bug outside your scope, report it in a `progress` note but don't fix it.")
    parts.append("")

    # ── 11. Completion ────────────────────────────────────────────────────────
    parts.append("## Completion")
    parts.append("")
    parts.append("Always push your branch before finishing — unpushed code has no value.")
    parts.append("Before posting your result, run `git status`. Your worktree MUST be clean.")
    parts.append("Stage and commit any intentional changes. Revert anything unintentional with `git checkout -- <file>`.")
    parts.append("")
    parts.append("**Sequence:**")
    parts.append("1. Ensure all checklist items are updated")
    parts.append("2. Run `git status` — worktree must be clean")
    parts.append(f"3. Push your branch: `mcp__switchboard__git_push(task_id='{task_id}')`")
    parts.append("4. Post a `handoff` message with key decisions, gotchas, and notes")
    parts.append(f"5. Post a `result` message (under 5 lines: what you did, files modified, caveats)")
    parts.append("")

    # ── 12. Pipeline & lifecycle ──────────────────────────────────────────────
    parts.append("## After You Finish: The Gate Pipeline")
    parts.append("")
    if task.get("auto_test") and project.get("test_command"):
        parts.append(f"1. **Test gate** — `{project['test_command']}` runs against your branch. If tests fail, you are retried with the failure output.")
    else:
        parts.append("1. **Test gate** — Your project's test suite runs against your branch. If tests fail, you are retried with the failure output.")
    parts.append("2. **Review gate** — An Opus instance reviews your diff against the spec. If changes are requested, you are retried with the review feedback.")
    parts.append("3. **Dependent tasks** — If your task has dependents, they dispatch automatically after your gates pass.")
    parts.append("")
    parts.append("You don't control the gates. If tests fail, you're retried with the failure output. "
                 "If review has feedback, you're retried with that feedback. Write clean code the first time.")
    parts.append("")

    # ── 13. Testing ───────────────────────────────────────────────────────────
    parts.append("## Testing")
    if task.get("auto_test") and project.get("test_command"):
        parts.append(f"Tests run automatically after you finish via `{project['test_command']}`. Do NOT run the full suite — the gate handles it.")
        parts.append("Run targeted tests during development to validate your changes.")
    elif project.get("test_command"):
        parts.append(f"Run tests with: `{project['test_command']}`")
    else:
        parts.append("No test command configured. If the project has tests, discover and run them.")
    parts.append("")
    parts.append("Write tests only if they add value and are not repeating existing coverage. "
                 "Bug fixes and refactors should verify existing tests pass, not add new ones. "
                 "Check what test coverage already exists before writing new tests.")
    parts.append("")

    # ── 14. Escalation ───────────────────────────────────────────────────────
    parts.append("## Escalation Protocol")
    parts.append(f"- **Stuck** → post a `question` message. Pauses your session until a human responds.")
    parts.append("- **Ambiguous spec** → post a question. Don't guess.")
    parts.append("- **Scope significantly larger than expected** → update phase to `needs-review` and explain.")
    parts.append(f"- **Fundamental blocker** → call `escalate(task_id='{task_id}', reason='...')` to flag for human review.")
    if escalation_criteria:
        parts.append("")
        parts.append(escalation_criteria)
    parts.append("")

    return "\n".join(parts)


async def _build_resume_prompt(task_id: str) -> str:
    """Build prompt for resuming a paused task. Re-grounds CC after potential context compaction."""
    task = await db.get_task(task_id)
    if not task:
        return (
            f"Resume task '{task_id}'. "
            f"Run `mcp__switchboard__read_task_messages(task_id='{task_id}')` for any new instructions, "
            f"then continue."
        )

    checklist = await db.get_checklist(task_id)

    parts = []
    parts.append(f"Resuming task `{task_id}` on branch `{task['branch']}`.")
    parts.append(f"Goal: {task['goal']}")
    parts.append("")

    if checklist:
        parts.append("## Current Checklist")
        for item in checklist:
            status = "✅" if item.get("done") else "⬜"
            parts.append(f"- {status} (item_id={item['id']}) {item['item']}")
        parts.append("")

    parts.append(f"Check task messages for any new instructions posted while you were paused: `mcp__switchboard__read_task_messages(task_id='{task_id}')`")
    parts.append("Then continue from where you left off.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Logging utilities
# ---------------------------------------------------------------------------

async def _setup_log_dir(worktree_path: str, clean: bool = True) -> Path:
    """Create .switchboard log directory in the worktree.

    Created as the worker user (who owns the worktree), with group-write
    so the service user can also write dispatch/session logs.

    When clean=True (dispatch/retry), removes stale files from previous tasks.
    When clean=False (resume), preserves existing logs for session continuity.

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
        log.debug(f"Removing git-tracked .switchboard files from {worktree_path}")
        await _run_as_worker("git", "-C", worktree_path, "rm", "-rf", "--cached", ".switchboard")

    # Ensure .switchboard is gitignored so CC never commits it
    gitignore_path = Path(worktree_path) / ".gitignore"
    if gitignore_path.exists():
        content = gitignore_path.read_text()
        if ".switchboard" not in content:
            await _run_as_worker("sh", "-c", f"echo '.switchboard/' >> {gitignore_path}")
    else:
        await _run_as_worker("sh", "-c", f"echo '.switchboard/' > {gitignore_path}")

    if clean:
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
                        model: str = "sonnet",
                        forked: bool = False,
                        fork_parent_session: str | None = None):
    """Write dispatch metadata to log file."""
    log_path = log_dir / "dispatch.log"
    with _open_shared(log_path) as f:
        f.write(f"[{db.now_iso()}] {'Resuming' if is_resume else 'Dispatching'} task {task_id}\n")
        f.write(f"  session_id: {session_id}\n")
        f.write(f"  model: {model}\n")
        f.write(f"  max_turns: {max_turns}\n")
        f.write(f"  max_wall_clock: {max_wall_clock}m\n")
        f.write(f"  worktree: {worktree_path}\n")
        f.write(f"  forked: {forked}\n")
        f.write(f"  fork_parent_session: {fork_parent_session}\n")


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
# Tool permission guard — block gh CLI
# ---------------------------------------------------------------------------

async def _gh_cli_guard(
    tool_name: str, tool_input: dict, context: ToolPermissionContext
) -> PermissionResultAllow | PermissionResultDeny:
    """Block Bash commands that invoke the gh CLI.

    gh commands are not allowed inside CC workers — PRs are created automatically
    by the gate pipeline. This prevents workers from running gh pr create or any
    other gh command.
    """
    if tool_name in ("Bash", "bash"):
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else str(tool_input)
        if (
            command.strip().startswith("gh ")
            or " gh " in command
            or "|gh " in command
            or "| gh " in command
        ):
            return PermissionResultDeny(
                message=(
                    "gh CLI is not allowed. PRs are created automatically by the gate pipeline. "
                    "Never use gh pr create or any gh command."
                )
            )
    return PermissionResultAllow()


async def _session_has_conversation(worker_home: str, worktree_path: str, session_id: str) -> bool:
    """Check if a CC session file has real conversation content (not just queue ops).

    Runs as the worker user since session files are owned by the worker (0600).
    """
    cwd_encoded = worktree_path.replace("/", "-").lstrip("-")
    session_file = Path(worker_home) / ".claude" / "projects" / f"-{cwd_encoded}" / f"{session_id}.jsonl"
    try:
        stdout, _, _ = await _run_as_worker(
            "python3", "-c", f"""
import json, sys
try:
    with open("{session_file}") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("type") not in ("queue-operation",):
                print("has_conversation")
                sys.exit(0)
    print("empty")
except FileNotFoundError:
    print("not_found")
except Exception as e:
    print(f"error:{{e}}")
""",
            cwd=worktree_path,
        )
        output = stdout.decode().strip() if stdout else ""
        if output == "has_conversation":
            return True
        if output == "not_found":
            log.warning("Session file not found: %s", session_file)
        elif output == "empty":
            log.warning("Session %s has no conversation content", session_id)
        return False
    except Exception as e:
        log.warning("Failed to check session file %s: %s", session_file, e)
        return True  # Can't check — let CC try


# ---------------------------------------------------------------------------
# Agent SDK Dispatch
# ---------------------------------------------------------------------------

async def _run_sdk_session(
    task_id: str, prompt: str, worktree_path: str,
    session_id: str | None, is_resume: bool,
    max_turns: int, max_wall_clock_minutes: int,
    log_dir: Path, model: str = "sonnet",
    fork_session_id: str | None = None,
) -> None:
    """Run a CC session via the Agent SDK. Blocks until complete."""
    # Lazy imports to break circular dependency
    from switchboard.dispatch.lifecycle import lifecycle
    from switchboard.dispatch._state import _active_clients

    stderr_path = log_dir / "cc-stderr.log"
    stderr_log = _open_shared(stderr_path)

    # Build SDK options — run CC as restricted 'switchboard' user
    worker_home = pwd.getpwnam(WORKER_USER).pw_dir

    # Merge user-level MCP servers from ~/.claude.json (e.g. shopify-ai)
    mcp_servers = {
        "switchboard": {
            "type": "http",
            "url": f"http://localhost:{os.environ.get('SWITCHBOARD_PORT', '8100')}/mcp/worker",
        },
    }
    try:
        with open(os.path.join(worker_home, ".claude.json")) as f:
            for name, cfg in json.load(f).get("mcpServers", {}).items():
                if name not in mcp_servers:
                    mcp_servers[name] = cfg
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        pass

    # Resolve Anthropic API key: dispatching user → instance owner → skip.
    # When SKIP_CREDENTIAL_CHECK is set and the worker has OAuth credentials,
    # prefer the CC subscription (don't inject API key into env).
    env = {"HOME": worker_home}
    _worker_has_oauth = False
    if SKIP_CREDENTIAL_CHECK:
        try:
            creds_path = str(Path(worker_home) / ".claude" / ".credentials.json")
            stdout, _, _ = await _run_as_worker(
                "python3", "-c",
                f"import json; d=json.load(open('{creds_path}')); "
                "o=d.get('claudeAiOauth',{}); print('yes' if o.get('accessToken') or o.get('refreshToken') else 'no')",
                cwd=worker_home,
            )
            _worker_has_oauth = (stdout.decode().strip() == "yes") if stdout else False
        except Exception as e:
            log.warning("OAuth check failed: %s", e)

    log.info("Auth resolution for %s: SKIP_CREDENTIAL_CHECK=%s, worker_has_oauth=%s", task_id, SKIP_CREDENTIAL_CHECK, _worker_has_oauth)

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
            port = os.environ.get("SWITCHBOARD_PORT", "8100")
            env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}/proxy/anthropic/{proxy_user_id}"
            env["ANTHROPIC_AUTH_TOKEN"] = "proxy"  # CC checks for presence; proxy injects the real key
        else:
            log.warning("No Anthropic API key resolved for task %s", task_id)

    options = ClaudeAgentOptions(
        user=WORKER_USER,
        cwd=str(worktree_path),
        env=env,
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
        disallowed_tools=["mcp__claude_ai_*"],
        debug_stderr=stderr_log,
        extra_args={"replay-user-messages": None},
        can_use_tool=_gh_cli_guard,
    )

    # If resuming, use the resume option — but only if the session file
    # has real conversation content (not just queue metadata)
    if is_resume and session_id:
        if await _session_has_conversation(worker_home, worktree_path, session_id):
            options.resume = session_id
        else:
            log.warning("Session %s has no conversation content, starting fresh", session_id)
    # If forking from a previous attempt's session
    elif fork_session_id:
        if await _session_has_conversation(worker_home, worktree_path, fork_session_id):
            options.resume = fork_session_id
            options.fork_session = True
        else:
            log.warning("Fork session %s has no conversation content, starting fresh", fork_session_id)

    # Capture the current attempt number for writing to attempt records
    _task_for_attempt = await db.get_task(task_id)
    _current_attempt = (_task_for_attempt.get("current_attempt") or 1) if _task_for_attempt else 1

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
                    log.debug(f"Injecting message {msg_id} into task {task_id}")
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

            actual_prompt = await _build_resume_prompt(task_id) if is_resume else prompt

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

                        # Capture session_id early from init so stop→resume works
                        if isinstance(message, SystemMessage) and getattr(message, 'subtype', None) == "init":
                            sid = (message.data or {}).get("session_id") if hasattr(message, 'data') else None
                            if sid:
                                await db.update_task(task_id, session_id=sid)
                                await db.update_attempt(task_id, _current_attempt, session_id=sid)

                        if isinstance(message, AssistantMessage):
                            turn_count += 1
                            for block in (message.content or []):
                                if isinstance(block, ToolUseBlock):
                                    last_tool_name = block.name

                        if isinstance(message, ResultMessage):
                            result_msg = message
                            if message.session_id:
                                await db.update_task(task_id, session_id=message.session_id)
                                await db.update_attempt(task_id, _current_attempt, session_id=message.session_id)
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
                    # Fallback: capture session_id via list_sessions if not yet stored
                    try:
                        task_check = await db.get_task(task_id)
                        if task_check and not task_check.get("session_id"):
                            from claude_agent_sdk import list_sessions
                            sessions = list_sessions(directory=worktree_path, limit=1)
                            if sessions:
                                sid = sessions[0].session_id
                                await db.update_task(task_id, session_id=sid)
                                await db.update_attempt(task_id, _current_attempt, session_id=sid)
                    except Exception:
                        pass

        try:
            await asyncio.wait_for(_run(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            log.warning(f"Task {task_id}: wall clock timeout ({max_wall_clock_minutes}m)")
            await lifecycle.execute(task_id, "timeout",
                triggered_by="system",
                source_detail=f"_run_sdk_session (timeout {max_wall_clock_minutes}m)",
                max_wall_clock_minutes=max_wall_clock_minutes,
            )
            with _open_shared(log_dir / "dispatch.log") as f:
                f.write(f"[{db.now_iso()}] Wall clock timeout ({max_wall_clock_minutes}m)\n")
            return

        # Process result
        if result_msg:
            _log_result(log_dir, result_msg)

            if result_msg.stop_reason == "max_turns" or (
                result_msg.num_turns and result_msg.num_turns >= max_turns
            ):
                project = await db.get_project((await db.get_task(task_id))["project_id"])
                await lifecycle.execute(task_id, "exhaust_turns",
                    triggered_by="system",
                    source_detail=f"_run_sdk_session (max_turns={max_turns})",
                    result_msg=result_msg,
                    project=project,
                    review_reason=f"Turns exhausted ({result_msg.num_turns}/{max_turns}). Resume to continue.",
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

                log.warning(f"Task {task_id}: rate limited, retry_after={retry_after_iso}")
                await lifecycle.execute(task_id, "rate_limit",
                    triggered_by="system",
                    source_detail="_run_sdk_session (API rate limit hit)",
                    retry_after=retry_after_iso,
                    reset_info=reset_info,
                    result_msg=result_msg,
                )
            elif result_msg.is_error:
                await lifecycle.execute(task_id, "error",
                    triggered_by="system",
                    source_detail=f"_run_sdk_session (error: {result_msg.stop_reason})",
                    result_msg=result_msg,
                )
            else:
                await lifecycle.execute(task_id, "complete",
                    triggered_by="system",
                    source_detail="_run_sdk_session (CC session completed)",
                    result_msg=result_msg,
                )
        else:
            # No result message — shouldn't happen but handle gracefully
            await lifecycle.execute(task_id, "error",
                triggered_by="system",
                source_detail="_run_sdk_session (no ResultMessage received)",
                reason="no_result",
            )

    except Exception as e:
        error_str = str(e)
        is_sigterm = any(s in error_str for s in ("exit code -15", "exit code -9", "exit code 143", "exit code 137"))

        if is_sigterm:
            log.warning(f"SDK session killed by signal for task {task_id}: {e}")
            await lifecycle.execute(task_id, "signal_kill",
                triggered_by="system",
                source_detail=f"_run_sdk_session (signal: {error_str[:200]})",
                error_message=error_str,
            )
        else:
            log.exception(f"SDK session error for task {task_id}: {e}")
            await lifecycle.execute(task_id, "error",
                triggered_by="system",
                source_detail=f"_run_sdk_session (exception: {error_str[:200]})",
                error_message=error_str,
                error_title="Dispatch error",
                error_content=f"SDK session raised an exception:\n\n```\n{e}\n```",
            )
        with _open_shared(log_dir / "dispatch.log") as f:
            f.write(f"[{db.now_iso()}] SDK error: {e}\n")
    finally:
        stderr_log.close()
