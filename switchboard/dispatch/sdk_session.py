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
from switchboard.config.settings import WORKER_USER
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
    parts.append(f"Dispatched by **{dispatched_by}** for project **{project_id}**.")
    parts.append(f"Branch: `{branch}` | Worktree: `{worktree_path}` | Task ID: `{task_id}`")
    parts.append("Your checklist updates, phase changes, and messages appear on a live dashboard. Update phase and checklist as you work.")
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

    # ── 5. Component context ─────────────────────────────────────────────────
    if task.get("component_id"):
        component = await db.get_component(task["component_id"])
        if component:
            parts.append("## Component Context")
            parts.append(f"**Component:** {component['name']}")
            if component.get("description"):
                parts.append(f"**Description:** {component['description']}")
            if component.get("phase"):
                parts.append(f"**Phase:** {component['phase']}")
            punchlist = await db.list_punchlist(task["component_id"])
            if punchlist:
                parts.append("")
                parts.append("**Punchlist items for this component:**")
                for p in punchlist:
                    status_label = p.get("status", "open")
                    parts.append(f"- (id={p['id']}) [{status_label}] {p['item']}")
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

    # ── 7. Tool inventory ────────────────────────────────────────────────────
    parts.append("## Your Tools")
    parts.append("")
    parts.append("### Progress Reporting")
    parts.append("Use constantly — update phase and checklist as you work, not just at the end.")
    parts.append("")
    parts.append("| Tool | When to use |")
    parts.append("|------|------------|")
    parts.append("| `update_task_checklist(item_id, done)` | Mark a checklist item complete. Do this as you finish each item. |")
    parts.append(f"| `update_task_phase(task_id, phase, detail)` | Update your phase on the dashboard (e.g., grounding, implementing, testing). |")
    parts.append("| `post_task_message(task_id, author='cc-worker', type, content)` | Post progress updates, questions, or results to your task thread. |")
    parts.append("")
    parts.append("**Message types:** `progress` (status updates) | `question` (blocks session until answered — use for blockers) | `plan` (post during grounding) | `result` (final summary) | `handoff` (notes for next task in chain)")
    parts.append("")
    parts.append("### Checklist Management")
    parts.append("Use during grounding and as scope evolves.")
    parts.append("")
    parts.append("| Tool | When to use |")
    parts.append("|------|------------|")
    parts.append("| `add_checklist_item(task_id, item)` | Add a deliverable you discovered during grounding or implementation. |")
    parts.append("| `remove_checklist_item(item_id)` | Remove an item that doesn't apply to this task. |")
    parts.append("| `update_checklist_item(item_id, item)` | Fix the text of an inaccurate checklist item. |")
    parts.append("")
    parts.append("### Context Discovery")
    parts.append("Use when the spec is unclear or you need to understand a prior design decision.")
    parts.append("")
    parts.append("| Tool | When to use |")
    parts.append("|------|------------|")
    parts.append(f"| `search(query, project_id='{project_id}')` | Search across tasks, conversations, and messages. Use for any search query. |")
    parts.append("| `read_task_messages(task_id)` | Read the full thread of a related task — useful for chain context. |")
    parts.append("| `get_task_status(task_id)` | Check the current status of any task. |")
    parts.append("| `get_pipeline(task_id)` | See your full dependency chain. |")
    parts.append("")
    parts.append("### Punchlist")
    parts.append("Use if your task is assigned to a component.")
    parts.append("")
    parts.append("| Tool | When to use |")
    parts.append("|------|------------|")
    parts.append("| `claim_punchlist_item(item_id, task_id)` | Claim a punchlist item you're working on. |")
    parts.append("| `resolve_punchlist_item(item_id)` | Mark a punchlist item done after completing the work. |")
    parts.append("| `add_punchlist_item(component_id, item)` | Report a new issue you discovered during work. |")
    parts.append("| `list_punchlist(component_id)` | See all open items for the component. |")
    parts.append("")
    parts.append("### File Operations")
    parts.append("Use for artifacts, reports, screenshots, and analysis outputs.")
    parts.append("")
    parts.append("| Tool | When to use |")
    parts.append("|------|------------|")
    parts.append("| `add_task_file(task_id, source_path)` | Persist a generated file for download. Pass absolute path. |")
    parts.append("| `list_task_files(task_id)` | Browse files attached to any task. |")
    parts.append("| `get_task_file(task_id, path)` | Read a specific file from any task's branch. |")
    parts.append("")

    # ── 8. Instructions + Git workflow ───────────────────────────────────────
    parts.append("## Instructions")
    parts.append("- You are working in an isolated git worktree. Commit freely to your branch.")
    parts.append("- Use the Ouvrage MCP tools to report progress:")
    parts.append(f"  - Update checklist: `mcp__switchboard__update_task_checklist(item_id=<id>, done=true)`")
    parts.append(f"  - Update phase: `mcp__switchboard__update_task_phase(task_id='{task_id}', phase='implementing', detail='...')`")
    parts.append(f"  - Post progress: `mcp__switchboard__post_task_message(task_id='{task_id}', author='cc-worker', type='progress', content='...')`")
    parts.append(f"  - Post question (will pause session): `mcp__switchboard__post_task_message(task_id='{task_id}', author='cc-worker', type='question', content='...')`")
    parts.append("- **Update each checklist item as you complete it.** This is how progress is tracked.")
    parts.append(f"- When done, commit your work, **push your branch** (`git push origin {branch}`), and post a result summary as type='result'.")
    parts.append("- **Always push your branch before finishing.** Your work is headless — unpushed code has no value.")
    parts.append("- Before finishing, post a handoff message with key decisions, gotchas, and notes for the next task:")
    parts.append(f"  `mcp__switchboard__post_task_message(task_id='{task_id}', author='cc-worker', type='handoff', content='...')`")
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
    parts.append("**Completion sequence:**")
    parts.append("1. Ensure all checklist items are updated")
    parts.append("2. Run `git status` — worktree must be clean")
    parts.append(f"3. `git push origin {branch}`")
    parts.append("4. Post a `handoff` message with key decisions, gotchas, and notes")
    parts.append("5. Post a `result` message (see Result Summary below)")
    parts.append("")

    parts.append("## Result Summary")
    parts.append("")
    parts.append("Before completing, post a `result` type message via `post_task_message`. Keep it under 5 lines. Include:")
    parts.append("")
    parts.append("1. **What you did** — 2-3 sentences describing the change")
    parts.append("2. **Files created or modified** — list the paths")
    parts.append("3. **Caveats or reviewer notes** — anything that needs attention or follow-up")
    parts.append("")
    parts.append("Do not dump full file contents. This summary is what a manager reads in `get_task_status` — make it useful at a glance.")
    parts.append("")

    parts.append("## SAFETY: Running tests and processes")
    parts.append("- Use `timeout 60 pytest ...` for targeted test runs — always wrap with timeout")
    parts.append("- NEVER use kill, pkill, or killall directly — you WILL terminate yourself")
    parts.append("- If a process hangs, let the timeout handle it or escalate to needs-review")
    parts.append("- Run targeted tests (specific files/functions) during development, the gate handles the full suite")
    parts.append("- If you need to stop a background process, use `timeout` on the original command instead")
    parts.append("")

    # ── 9. Pipeline awareness ─────────────────────────────────────────────────
    parts.append("## After You Finish: The Gate Pipeline")
    parts.append("")
    if task.get("auto_test") and project.get("test_command"):
        parts.append(f"1. **Test gate** — `{project['test_command']}` runs against your branch. If tests fail, you are retried with the failure output.")
    else:
        parts.append("1. **Test gate** — Your project's test suite runs against your branch. If tests fail, you are retried with the failure output.")
    parts.append("2. **Review gate** — An Opus instance reviews your diff against the spec. If changes are requested, you are retried with the review feedback.")
    parts.append("3. **Dependent tasks** — If your task has dependents, they dispatch automatically after your gates pass.")
    parts.append("4. You don't control the gates. Write clean code, write passing tests.")
    parts.append("")

    # ── 10. Testing ───────────────────────────────────────────────────────────
    parts.append("## Testing")
    if task.get("auto_test") and project.get("test_command"):
        parts.append(f"Tests run automatically after you finish via `{project['test_command']}`. Do NOT run the full suite — the gate handles it.")
        parts.append("Run targeted tests during development to validate your changes. Ensure tests you write pass before moving on.")
    elif project.get("test_command"):
        parts.append(f"Run tests with: `{project['test_command']}`")
        parts.append("Write tests for new functionality. Run them and ensure they pass before marking checklist items done.")
    else:
        parts.append("No test command configured. If the project has tests, discover and run them. Write tests for new functionality and verify they pass.")
    parts.append("")

    # ── 11. Escalation protocol ───────────────────────────────────────────────
    parts.append("## Escalation Protocol")
    parts.append(f"- **Stuck** → post a `question` message (`mcp__switchboard__post_task_message(task_id='{task_id}', author='cc-worker', type='question', content='...')`). Pauses your session until a human responds.")
    parts.append("- **Ambiguous spec** → post a question. Don't guess.")
    parts.append("- **Scope significantly larger than expected** → update phase to `needs-review` and explain.")
    parts.append("- **Blocking issue** (missing access, broken dependency) → post a question immediately.")
    parts.append(f"- **Fundamental blocker you cannot resolve** → call `escalate(task_id='{task_id}', reason='...')` to flag the task for human review. Do not try to work around fundamental blockers.")
    if escalation_criteria:
        parts.append("")
        parts.append(escalation_criteria)
    parts.append("")

    # ── 12. What NOT to do ────────────────────────────────────────────────────
    parts.append("## What NOT To Do")
    parts.append("- No `kill`/`pkill`/`killall` — use `timeout` for process management.")
    parts.append("- Don't run `gh` CLI commands — PRs are created automatically by the gate pipeline. Never run `gh pr create` or any `gh` command.")
    if task.get("auto_test") and project.get("test_command"):
        parts.append("- No running the full test suite — the gate handles it. Run targeted tests only.")
    parts.append("- No `git config` changes — config is shared across all worktrees.")
    parts.append("- No checking out other branches — you own your branch only.")
    parts.append("- No guessing when stuck — post a question.")
    parts.append("- No adding frameworks unless the spec explicitly requires it.")
    parts.append("- No committing secrets (API keys, credentials, .env files) to git.")
    parts.append("")

    # ── 13. Grounding phase (skip for revision retries) ──────────────────────
    if not review_feedback:
        parts.append("## Grounding Phase")
        parts.append("GROUNDING PHASE (do this BEFORE coding):")
        parts.append("1. Read the relevant source files for this task")
        parts.append("2. Review the spec — understand WHY this is being requested, not just WHAT")
        parts.append("3. Review each deliverable in the checklist against the actual code")
        parts.append("4. Adjust deliverables using the checklist tools: fix inaccuracies, add missing items, remove irrelevant ones. Small adjustments are fine to make silently.")
        parts.append("5. If the approach fundamentally won't work, scope is significantly larger than expected, or you see a better way to achieve the goal → set status to needs-review and explain")
        parts.append(f"6. Post your implementation plan as a type='plan' message with file-level detail: `mcp__switchboard__post_task_message(task_id='{task_id}', author='cc-worker', type='plan', content='...')`")
        parts.append("7. Then begin coding")
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
        can_use_tool=_gh_cli_guard,
    )

    # If resuming, use the resume option
    if is_resume and session_id:
        options.resume = session_id
    # If forking from a previous attempt's session
    elif fork_session_id:
        options.resume = fork_session_id
        options.fork_session = True

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
