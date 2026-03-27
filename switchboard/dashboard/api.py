"""Dashboard REST API — JSON endpoints for the Switchboard SPA."""

import json
import logging
import os
import secrets
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, unquote

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

import switchboard.db as db
import switchboard.dispatch as tasks
from switchboard.auth.oauth import get_client as _get_oauth_client
from switchboard.config.constants import DEFAULT_MAX_CONCURRENT
from switchboard.crypto import decrypt_value, encrypt_value, is_fernet_token
from switchboard.notifications import web_push

logger = logging.getLogger(__name__)
_start_time = time.monotonic()
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "").rstrip("/")


# ── Helpers ───────────────────────────────────────────────────────────────

async def _read_body(receive) -> bytes:
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    return body


async def _json_response(send, data, status=200):
    body = json.dumps(data, default=str).encode()
    await send({
        "type": "http.response.start", "status": status,
        "headers": [
            [b"content-type", b"application/json"],
            [b"cache-control", b"no-cache"],
        ],
    })
    await send({"type": "http.response.body", "body": body})


async def _text_response(send, text, status=200):
    await send({
        "type": "http.response.start", "status": status,
        "headers": [[b"content-type", b"text/plain"]],
    })
    await send({"type": "http.response.body", "body": text.encode()})


async def _error(send, message, status=400):
    await _json_response(send, {"error": message}, status)


def _parse_qs(scope) -> dict:
    qs = scope.get("query_string", b"").decode()
    parsed = parse_qs(qs, keep_blank_values=False)
    # Flatten: parse_qs returns lists; we only need the first value per key
    return {k: v[0] for k, v in parsed.items() if v}


def _extract_task_id(path: str, prefix: str) -> str:
    """Extract task_id from path after prefix. Handles URL-encoded slashes."""
    rest = unquote(path[len(prefix):])
    # Strip trailing action segments like /cancel, /retry, /resume, /messages, /session-log, /dispatch-log
    for suffix in ("/cancel", "/retry", "/resume", "/close", "/skip-gate",
                    "/advance-chain", "/cancel-chain", "/approve", "/chain",
                    "/review-task", "/messages", "/session-log", "/dispatch-log",
                    "/attempts", "/dispatch", "/reopen", "/cancel-reopen", "/start",
                    "/test-output", "/gate-session-log"):
        if rest.endswith(suffix):
            return rest[:-len(suffix)]
    return rest


# ── Route dispatcher ──────────────────────────────────────────────────────

async def handle_request(scope, receive, send):
    """Main dispatcher for /dashboard/api/* routes."""
    path = scope["path"]
    method = scope.get("method", "GET")

    try:
        # GET /dashboard/api/system
        if path == "/dashboard/api/system" and method == "GET":
            return await _handle_system(send)

        # GET /dashboard/api/projects
        if path == "/dashboard/api/projects" and method == "GET":
            return await _handle_list_projects(send)

        # /dashboard/api/projects/{id}[/pause|resume|stop]
        if path.startswith("/dashboard/api/projects/"):
            rest = path[len("/dashboard/api/projects/"):]
            if method == "GET" and "/" not in rest:
                return await _handle_get_project(send, rest)
            if method == "POST" and rest.endswith("/pause"):
                pid = rest[:-len("/pause")]
                result = await tasks.pause_project(pid)
                return await _json_response(send, result)
            if method == "POST" and rest.endswith("/resume"):
                pid = rest[:-len("/resume")]
                result = await tasks.resume_project(pid)
                return await _json_response(send, result)
            if method == "POST" and rest.endswith("/stop"):
                pid = rest[:-len("/stop")]
                result = await tasks.stop_project(pid)
                return await _json_response(send, result)

        # GET /dashboard/api/components
        if path == "/dashboard/api/components" and method == "GET":
            return await _handle_list_components(scope, send)

        # /dashboard/api/components/{id} — GET detail, PATCH update, /activity
        if path.startswith("/dashboard/api/components/"):
            rest = path[len("/dashboard/api/components/"):]
            if method == "GET" and rest.endswith("/activity"):
                component_id = rest[:-len("/activity")]
                return await _handle_get_component_activity(send, component_id)
            if method == "GET" and "/" not in rest:
                return await _handle_get_component(send, rest)
            if method == "PATCH" and "/" not in rest:
                return await _handle_update_component(receive, send, rest)
            if method == "POST" and rest.endswith("/pause"):
                cid = rest[:-len("/pause")]
                result = await tasks.pause_component(cid)
                return await _json_response(send, result)
            if method == "POST" and rest.endswith("/resume"):
                cid = rest[:-len("/resume")]
                result = await tasks.resume_component(cid)
                return await _json_response(send, result)
            if method == "POST" and rest.endswith("/stop"):
                cid = rest[:-len("/stop")]
                result = await tasks.stop_component(cid)
                return await _json_response(send, result)

        # Punchlist routes: /dashboard/api/punchlist/{component_id}[/{item_id}[/dispatch]]
        if path.startswith("/dashboard/api/punchlist/"):
            rest = path[len("/dashboard/api/punchlist/"):]
            parts = rest.split("/")
            component_id = parts[0]

            if method == "GET" and len(parts) == 1:
                return await _handle_list_punchlist(send, component_id)
            if method == "POST" and len(parts) == 1:
                return await _handle_create_punchlist_item(receive, send, component_id)
            if method == "PATCH" and len(parts) == 2:
                return await _handle_update_punchlist_item(receive, send, component_id, int(parts[1]))
            if method == "DELETE" and len(parts) == 2:
                return await _handle_delete_punchlist_item(send, int(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[2] == "dispatch":
                return await _handle_dispatch_punchlist_item(receive, send, component_id, int(parts[1]))

        # GET /dashboard/api/conversations
        if path == "/dashboard/api/conversations" and method == "GET":
            return await _handle_list_conversations(scope, send)

        # GET/POST /dashboard/api/conversations/{id}[/messages]
        if path.startswith("/dashboard/api/conversations/"):
            rest = path[len("/dashboard/api/conversations/"):]
            if method == "POST" and rest.endswith("/messages"):
                conv_id = unquote(rest[:-len("/messages")])
                return await _handle_post_conversation_message(receive, send, conv_id)
            if method == "GET":
                conv_id = unquote(rest)
                return await _handle_get_conversation(scope, send, conv_id)

        # GET /dashboard/api/activity
        if path == "/dashboard/api/activity" and method == "GET":
            return await _handle_activity(scope, send)

        # Push subscription endpoints
        if path == "/dashboard/api/push/subscribe" and method == "POST":
            return await _handle_push_subscribe(receive, send)
        if path == "/dashboard/api/push/unsubscribe" and method == "POST":
            return await _handle_push_unsubscribe(receive, send)

        # Notification settings endpoints
        if path == "/dashboard/api/settings/notifications" and method == "GET":
            return await _handle_get_notification_settings(send)
        if path == "/dashboard/api/settings/notifications" and method == "POST":
            return await _handle_update_notification_settings(receive, send)

        # Push public key (needed by browser to subscribe)
        if path == "/dashboard/api/push/vapid-public-key" and method == "GET":
            return await _handle_vapid_public_key(send)

        # GET /dashboard/api/tasks
        if path == "/dashboard/api/tasks" and method == "GET":
            return await _handle_list_tasks(scope, send)

        # Task-specific routes: /dashboard/api/tasks/{task_id}[/action]
        if path.startswith("/dashboard/api/tasks/"):
            rest = path[len("/dashboard/api/tasks/"):]

            # POST actions
            if method == "POST":
                if rest.endswith("/cancel"):
                    task_id = rest[:-len("/cancel")]
                    return await _handle_cancel(send, task_id)
                if rest.endswith("/retry"):
                    task_id = rest[:-len("/retry")]
                    return await _handle_retry(receive, send, task_id)
                if rest.endswith("/resume"):
                    task_id = rest[:-len("/resume")]
                    return await _handle_resume(send, task_id)
                if rest.endswith("/close"):
                    task_id = rest[:-len("/close")]
                    return await _handle_close(receive, send, task_id)
                if rest.endswith("/skip-gate"):
                    task_id = rest[:-len("/skip-gate")]
                    return await _handle_skip_gate(send, task_id)
                if rest.endswith("/advance-chain"):
                    task_id = rest[:-len("/advance-chain")]
                    return await _handle_advance_chain(send, task_id)
                if rest.endswith("/cancel-chain"):
                    task_id = rest[:-len("/cancel-chain")]
                    return await _handle_cancel_chain(send, task_id)
                if rest.endswith("/hold"):
                    task_id = rest[:-len("/hold")]
                    return await _handle_hold(send, task_id)
                if rest.endswith("/approve"):
                    task_id = rest[:-len("/approve")]
                    return await _handle_approve(send, task_id)
                if rest.endswith("/release-worktree"):
                    task_id = rest[:-len("/release-worktree")]
                    return await _handle_release_worktree(send, task_id)
                if rest.endswith("/dispatch"):
                    task_id = rest[:-len("/dispatch")]
                    return await _handle_dispatch(send, task_id)
                if rest.endswith("/reopen"):
                    task_id = rest[:-len("/reopen")]
                    return await _handle_reopen(send, task_id)
                if rest.endswith("/cancel-reopen"):
                    task_id = rest[:-len("/cancel-reopen")]
                    return await _handle_cancel_reopen(send, task_id)
                if rest.endswith("/start"):
                    task_id = rest[:-len("/start")]
                    return await _handle_start(receive, send, task_id)
                if rest.endswith("/messages"):
                    task_id = rest[:-len("/messages")]
                    return await _handle_post_message(receive, send, task_id)

            # GET sub-resources
            if method == "GET":
                if rest.endswith("/messages"):
                    task_id = rest[:-len("/messages")]
                    return await _handle_get_messages(scope, send, task_id)
                if rest.endswith("/session-log"):
                    task_id = rest[:-len("/session-log")]
                    return await _handle_session_log(scope, send, task_id)
                if rest.endswith("/dispatch-log"):
                    task_id = rest[:-len("/dispatch-log")]
                    return await _handle_dispatch_log(scope, send, task_id)
                if rest.endswith("/attempts"):
                    task_id = rest[:-len("/attempts")]
                    return await _handle_get_attempts(send, task_id)
                if rest.endswith("/chain"):
                    task_id = rest[:-len("/chain")]
                    return await _handle_get_chain(send, task_id)
                if rest.endswith("/review-task"):
                    task_id = rest[:-len("/review-task")]
                    return await _handle_get_review_task(send, task_id)
                if rest.endswith("/test-output"):
                    task_id = rest[:-len("/test-output")]
                    return await _handle_test_output(scope, send, task_id)
                if rest.endswith("/gate-session-log"):
                    task_id = rest[:-len("/gate-session-log")]
                    return await _handle_gate_session_log(scope, send, task_id)

                # GET /dashboard/api/tasks/{task_id} (detail)
                return await _handle_get_task(send, rest)

        # ── Settings endpoints ─────────────────────────────────────────────
        # Instance settings (owner/admin only)
        if path == "/dashboard/api/settings/instance" and method == "GET":
            return await _handle_get_instance_settings(scope, send)
        if path == "/dashboard/api/settings/instance" and method == "PATCH":
            return await _handle_patch_instance_settings(receive, send, scope)
        if path == "/dashboard/api/settings/instance/test-github" and method == "POST":
            return await _handle_test_github(send, scope)
        if path == "/dashboard/api/settings/instance/regenerate-oauth-secret" and method == "POST":
            return await _handle_regenerate_oauth_secret(send, scope)

        # User settings (each user sees their own)
        if path == "/dashboard/api/settings/user" and method == "GET":
            return await _handle_get_user_settings(scope, send)
        if path == "/dashboard/api/settings/user" and method == "PATCH":
            return await _handle_patch_user_settings(receive, send, scope)
        if path == "/dashboard/api/settings/user/test-anthropic" and method == "POST":
            return await _handle_test_anthropic(send, scope)
        if path == "/dashboard/api/settings/user/change-password" and method == "POST":
            return await _handle_change_password(receive, send, scope)

        # ── Files endpoints ────────────────────────────────────────────────
        if path == "/dashboard/api/files" and method == "GET":
            return await _handle_list_files(send)
        if path == "/dashboard/api/files" and method == "POST":
            return await _handle_upload_file(scope, receive, send)
        if path.startswith("/dashboard/api/files/"):
            file_id = path[len("/dashboard/api/files/"):]
            if method == "PATCH":
                return await _handle_rename_file(receive, send, file_id, scope)
            if method == "DELETE":
                return await _handle_delete_file(send, file_id, scope)

        await _error(send, "Not found", 404)

    except ValueError as e:
        await _error(send, str(e), 404)
    except RuntimeError as e:
        await _error(send, str(e), 409)
    except Exception as e:
        await _error(send, f"Internal error: {e}", 500)


# ── Handlers ──────────────────────────────────────────────────────────────

async def _handle_system(send):
    active = await db.count_active_tasks()
    projects = await db.list_projects()
    all_tasks = await db.list_tasks()
    total_cost = sum(t.get("total_cost_usd", 0) or 0 for t in all_tasks)
    await _json_response(send, {
        "active_tasks": active,
        "max_concurrent": DEFAULT_MAX_CONCURRENT,
        "total_cost_usd": round(total_cost, 2),
        "uptime_seconds": round(time.monotonic() - _start_time),
        "jira_base_url": JIRA_BASE_URL or None,
    })


async def _handle_list_projects(send):
    projects = await db.list_projects()
    # Enrich with task counts using a single GROUP BY query instead of N+1
    counts = await db.get_project_task_counts()
    for p in projects:
        stats = counts.get(p["id"], {"total_tasks": 0, "active_task_count": 0, "total_cost": 0})
        p["active_task_count"] = stats["active_task_count"]
        p["total_tasks"] = stats["total_tasks"]
        p["total_cost"] = stats["total_cost"]
    await _json_response(send, projects)


async def _handle_get_project(send, project_id):
    project = await db.get_project(project_id)
    if not project:
        return await _error(send, f"Project '{project_id}' not found", 404)
    task_list = await db.list_tasks(project_id=project_id)
    project["tasks"] = task_list
    await _json_response(send, project)


async def _handle_activity(scope, send):
    params = _parse_qs(scope)
    project_id = params.get("project_id")
    try:
        limit = int(params.get("limit", 30))
        offset = int(params.get("offset", 0))
    except (ValueError, TypeError):
        return await _error(send, "limit and offset must be integers", 400)
    if limit < 0 or offset < 0:
        return await _error(send, "limit and offset must be non-negative", 400)
    limit = min(limit, 100)
    events = await db.get_activity(project_id=project_id, limit=limit, offset=offset)
    await _json_response(send, events)


async def _handle_list_tasks(scope, send):
    params = _parse_qs(scope)
    task_list = await db.list_tasks(
        project_id=params.get("project_id"),
        status=params.get("status"),
        tag=params.get("tag"),
    )
    # Sort: working first, then by last_activity desc
    def sort_key(t):
        is_working = 0 if t["status"] == "working" else 1
        return (is_working, t.get("last_activity") or t.get("updated_at") or "")
    task_list.sort(key=sort_key)
    # Reverse non-working group so most recent is first
    working = [t for t in task_list if t["status"] == "working"]
    rest = sorted(
        [t for t in task_list if t["status"] != "working"],
        key=lambda t: t.get("last_activity") or t.get("updated_at") or "",
        reverse=True,
    )
    await _json_response(send, working + rest)


async def _handle_get_task(send, task_id):
    task = await db.get_task(task_id)
    if not task:
        return await _error(send, f"Task '{task_id}' not found", 404)

    # Enrich with checklist, messages, artifacts
    checklist = await db.get_checklist(task_id)
    task["checklist"] = checklist
    task["checklist_total"] = len(checklist)
    task["checklist_done"] = sum(1 for c in checklist if c["done"])

    thread = await db.read_task_messages(task_id)
    task["messages"] = thread.get("messages", [])

    task["artifacts"] = await db.get_artifacts(task_id)
    task["tags"] = await db.get_task_tags(task_id)
    task["subtasks"] = await db.get_subtasks(task_id)

    # Check if PID is alive
    pid = task.get("pid")
    task["alive"] = bool(pid and tasks._is_pid_alive(pid))

    # Parse last_test_output JSON if stored as string
    if task.get("last_test_output") and isinstance(task["last_test_output"], str):
        try:
            task["last_test_output"] = json.loads(task["last_test_output"])
        except (json.JSONDecodeError, TypeError):
            pass

    # Add review_subtask from subtasks table
    task["review_subtask"] = await _get_review_subtask(task_id)

    # Add resolved config (inheritance-resolved: task → component → project → defaults)
    try:
        task["resolved_config"] = await db.resolve_config(task_id)
    except Exception:
        logger.debug("Failed to resolve config for task %s", task_id, exc_info=True)

    # Add project default_branch for git flow display
    try:
        project = await db.get_project(task["project_id"])
        if project:
            task["project_default_branch"] = project.get("default_branch", "main")
    except Exception:
        logger.debug("Failed to get project default_branch for task %s", task_id, exc_info=True)

    await _json_response(send, task)


async def _handle_get_messages(scope, send, task_id):
    params = _parse_qs(scope)
    last_n = min(int(params["limit"]), 200) if "limit" in params else None
    after = int(params["after"]) if "after" in params else None
    thread = await db.read_task_messages(task_id, last_n=last_n, after=after)
    await _json_response(send, thread)


async def _resolve_dashboard_log_dir(task: dict, attempt: int | None):
    """Return a Path to the .switchboard/ dir to read logs from.

    Priority: specific attempt archive → live worktree (DB path) → orphaned
    worktree dir (DB cleared but dir still on disk) → highest archive → None.
    """
    from pathlib import Path

    project = await db.get_project(task["project_id"])

    def _try_worktree_dir():
        """Check both DB worktree_path and physical dir on disk."""
        # DB still has the path
        wt = task.get("worktree_path")
        if wt:
            live = Path(wt) / ".switchboard"
            if live.exists():
                return live
        # DB cleared but dir might still exist (release failed or detach-only)
        if project:
            slug = tasks._task_slug(task["id"])
            orphan = Path(project["working_dir"]) / slug / ".switchboard"
            if orphan.exists():
                return orphan
        return None

    if attempt is not None:
        if project:
            archive = tasks._find_archive_path(project, task["id"], attempt)
            if archive:
                return archive
        # No archive — fall back to worktree if this is the current attempt
        current_attempt = task.get("current_attempt") or task.get("dispatch_count") or 1
        if attempt == current_attempt:
            return _try_worktree_dir()
        return None

    # Try worktree first
    wt_dir = _try_worktree_dir()
    if wt_dir:
        return wt_dir

    # Fall back to highest-numbered archive
    if project:
        archive = tasks._find_archive_path(project, task["id"], None)
        if archive:
            return archive

    return None


async def _handle_session_log(scope, send, task_id):
    params = _parse_qs(scope)
    attempt = int(params["attempt"]) if "attempt" in params else None

    task = await db.get_task(task_id)
    if not task:
        return await _error(send, f"Task '{task_id}' not found", 404)

    log_dir = await _resolve_dashboard_log_dir(task, attempt)
    if log_dir is None:
        return await _json_response(send, [])

    log_path = log_dir / "session.jsonl"
    if not log_path.exists():
        return await _json_response(send, [])

    entries = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception:
        pass

    await _json_response(send, entries)


async def _handle_dispatch_log(scope, send, task_id):
    params = _parse_qs(scope)
    attempt = int(params["attempt"]) if "attempt" in params else None

    task = await db.get_task(task_id)
    if not task:
        return await _error(send, f"Task '{task_id}' not found", 404)

    log_dir = await _resolve_dashboard_log_dir(task, attempt)
    if log_dir is None:
        return await _text_response(send, "")

    log_path = log_dir / "dispatch.log"
    if not log_path.exists():
        return await _text_response(send, "")

    try:
        with open(log_path) as f:
            return await _text_response(send, f.read())
    except Exception:
        return await _text_response(send, "")


async def _handle_test_output(scope, send, task_id):
    """Serve the live test output log file (.switchboard/test-output.log)."""
    task = await db.get_task(task_id)
    if not task:
        return await _error(send, f"Task '{task_id}' not found", 404)

    log_dir = await _resolve_dashboard_log_dir(task, None)
    if log_dir is None:
        return await _text_response(send, "")

    log_path = log_dir / "test-output.log"
    if not log_path.exists():
        return await _text_response(send, "")

    try:
        with open(log_path) as f:
            return await _text_response(send, f.read())
    except Exception:
        return await _text_response(send, "")


async def _handle_gate_session_log(scope, send, task_id):
    """Serve a subtask's session log (e.g. review-1-session.jsonl).

    Query params:
      - type: subtask type (default "review")
    """
    task = await db.get_task(task_id)
    if not task:
        return await _error(send, f"Task '{task_id}' not found", 404)

    params = _parse_qs(scope)
    subtask_type = params.get("type", "review")

    # Find the most recent subtask of this type to determine the count
    subtasks = await db.get_subtasks(task_id)
    type_subtasks = [s for s in subtasks if s.get("type") == subtask_type]
    if not type_subtasks:
        return await _json_response(send, [])

    count = len(type_subtasks)
    filename = f"{subtask_type}-{count}-session.jsonl"

    log_dir = await _resolve_dashboard_log_dir(task, None)
    if log_dir is None:
        return await _json_response(send, [])

    log_path = log_dir / filename
    if not log_path.exists():
        return await _json_response(send, [])

    entries = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except Exception:
        pass

    await _json_response(send, entries)


async def _handle_get_attempts(send, task_id):
    try:
        attempts = await db.get_task_attempts(task_id)
    except ValueError as e:
        return await _error(send, str(e), 404)
    await _json_response(send, {"task_id": task_id, "attempts": attempts})


# ── Actions ───────────────────────────────────────────────────────────────

async def _handle_cancel(send, task_id):
    result = await tasks.cancel_task(task_id)
    await _json_response(send, result)


async def _handle_retry(receive, send, task_id):
    body = await _read_body(receive)
    data = json.loads(body) if body else {}
    result = await tasks.retry_task(task_id, clean=data.get("clean", False))
    await _json_response(send, result)


async def _handle_resume(send, task_id):
    result = await tasks.resume_task(task_id)
    await _json_response(send, result)


async def _handle_approve(send, task_id):
    result = await tasks.approve_task(task_id)
    await _json_response(send, result)


async def _handle_dispatch(send, task_id):
    task = await db.get_task(task_id)
    if not task:
        return await _error(send, f"Task '{task_id}' not found", 404)
    if task["status"] != "ready":
        return await _error(send, f"Task is '{task['status']}', expected 'ready'", 400)
    result = await tasks.dispatch_task(
        project_id=task["project_id"],
        task_id=task_id,
        goal=task["goal"],
    )
    await _json_response(send, result)


async def _handle_close(receive, send, task_id):
    body = await _read_body(receive)
    data = json.loads(body) if body else {}
    result = await tasks.close_task(
        task_id=task_id,
        cleanup=data.get("cleanup", True),
        force_delete_branch=data.get("force_delete_branch", False),
    )
    await _json_response(send, result)



async def _handle_release_worktree(send, task_id):
    result = await tasks.release_worktree(task_id)
    await _json_response(send, result)


async def _handle_hold(send, task_id):
    task = await db.get_task(task_id)
    if not task:
        return await _error(send, f"Task '{task_id}' not found", 404)
    if task["status"] != "ready":
        return await _error(send, f"Cannot hold a task with status '{task['status']}' — only ready tasks can be held", 400)
    result = await db.update_task(task_id, held=True)
    await _json_response(send, result)


async def _handle_skip_gate(send, task_id):
    result = await tasks.skip_gate(task_id)
    await _json_response(send, result)


async def _handle_advance_chain(send, task_id):
    result = await tasks.advance_chain(task_id)
    await _json_response(send, result)


async def _handle_cancel_chain(send, task_id):
    result = await tasks.cancel_chain(task_id)
    await _json_response(send, result)


async def _handle_reopen(send, task_id):
    result = await tasks.reopen_task(task_id)
    await _json_response(send, result)


async def _handle_start(receive, send, task_id):
    body = await _read_body(receive)
    params = json.loads(body) if body else {}
    auto_test = params.get("auto_test")
    auto_review = params.get("auto_review")
    result = await tasks.start_reopened_task(
        task_id,
        auto_test=bool(auto_test) if auto_test is not None else None,
        auto_review=bool(auto_review) if auto_review is not None else None,
    )
    await _json_response(send, result)


async def _handle_cancel_reopen(send, task_id):
    result = await tasks.cancel_reopen(task_id)
    await _json_response(send, result)


async def _handle_get_chain(send, task_id):
    chain = await db.get_chain(task_id)
    current_index = next((i for i, t in enumerate(chain) if t["id"] == task_id), -1)
    await _json_response(send, {"chain": chain, "current_index": current_index})


async def _handle_get_review_task(send, task_id):
    """Find the review sub-task for a given task."""
    # Review tasks have parent_task_id pointing to this task
    async with db.get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
    if not rows:
        await _json_response(send, None)
        return
    review_task = dict(rows[0])
    # Get the review message posted on the parent
    msgs = await db.read_task_messages(task_id, type="review")
    review_msgs = [m for m in msgs.get("messages", []) if m.get("type") == "review"]
    review_task["review_message"] = review_msgs[-1] if review_msgs else None
    await _json_response(send, review_task)


async def _get_review_subtask(task_id: str) -> dict | None:
    """Get the most recent review subtask for a task."""
    from datetime import datetime, timezone
    async with db.get_db() as conn:
        rows = await conn.execute_fetchall(
            """SELECT id, status, model, created_at, completed_at
               FROM subtasks WHERE task_id = ? AND type = 'review'
               ORDER BY rowid DESC LIMIT 1""",
            (task_id,),
        )
    if not rows:
        return None
    rs = dict(rows[0])
    now_dt = datetime.now(timezone.utc)
    created_dt = datetime.fromisoformat(rs["created_at"].replace("Z", "+00:00"))
    if rs["status"] == "working" or not rs["completed_at"]:
        elapsed_s = int((now_dt - created_dt).total_seconds())
    else:
        completed_dt = datetime.fromisoformat(rs["completed_at"].replace("Z", "+00:00"))
        elapsed_s = int((completed_dt - created_dt).total_seconds())
    return {
        "task_id": rs["id"],
        "status": rs["status"],
        "session_id": None,
        "elapsed": f"{elapsed_s}s",
        "model": rs["model"],
    }


async def _handle_post_message(receive, send, task_id):
    body = await _read_body(receive)
    if not body:
        return await _error(send, "Request body required")
    data = json.loads(body)
    content = data.get("content", "").strip()
    if not content:
        return await _error(send, "content is required")

    result = await db.post_task_message(
        task_id=task_id,
        author="dashboard",
        content=content,
        type=data.get("type", "review"),
        title=data.get("title"),
    )
    await _json_response(send, result, 201)


# ── Conversations ────────────────────────────────────────────────────────

async def _handle_list_conversations(scope, send):
    params = _parse_qs(scope)
    conversations = await db.list_conversations(
        project=params.get("project"),
        search=params.get("search"),
    )
    await _json_response(send, conversations)


async def _handle_post_conversation_message(receive, send, conv_id):
    body = await _read_body(receive)
    if not body:
        return await _error(send, "Request body required")
    data = json.loads(body)
    content = data.get("content", "").strip()
    if not content:
        return await _error(send, "content is required")

    try:
        result = await db.post_message(
            conversation_id=conv_id,
            author="dashboard",
            content=content,
            type=data.get("type", "note"),
            title=data.get("title"),
        )
    except ValueError as e:
        return await _error(send, str(e), 404)
    await _json_response(send, result)


async def _handle_get_conversation(scope, send, conv_id):
    params = _parse_qs(scope)
    # Read conversation messages
    try:
        last_n = int(params["limit"]) if "limit" in params else None
        after = int(params["after"]) if "after" in params else None
        thread = await db.read_messages(conv_id, last_n=last_n, after=after)
    except ValueError as e:
        return await _error(send, str(e), 404)

    await _json_response(send, thread)


# ── Components ────────────────────────────────────────────────────────────

async def _handle_list_components(scope, send):
    params = _parse_qs(scope)
    project_id = params.get("project_id")
    if not project_id:
        return await _error(send, "project_id is required")
    components = await db.list_components(project_id)
    await _json_response(send, components)


async def _handle_get_component(send, component_id):
    component = await db.get_component(component_id)
    if not component:
        return await _error(send, f"Component '{component_id}' not found", 404)
    await _json_response(send, component)


async def _handle_update_component(receive, send, component_id):
    body = await _read_body(receive)
    data = json.loads(body) if body else {}
    result = await db.update_component(component_id, **data)
    await _json_response(send, result)


async def _handle_get_component_activity(send, component_id):
    events = await db.get_component_activity(component_id)
    await _json_response(send, events)


# ── Punchlist ─────────────────────────────────────────────────────────────

async def _handle_list_punchlist(send, component_id):
    items = await db.list_punchlist(component_id)
    await _json_response(send, items)


async def _handle_create_punchlist_item(receive, send, component_id):
    body = await _read_body(receive)
    data = json.loads(body) if body else {}
    item_text = data.get("item", "").strip()
    if not item_text:
        return await _error(send, "item is required")
    result = await db.create_punchlist_item(component_id, item_text)
    await _json_response(send, result, 201)


async def _handle_update_punchlist_item(receive, send, component_id, item_id):
    body = await _read_body(receive)
    data = json.loads(body) if body else {}
    result = await db.update_punchlist_item(item_id, **data)
    await _json_response(send, result)


async def _handle_delete_punchlist_item(send, item_id):
    await db.delete_punchlist_item(item_id)
    await _json_response(send, {"ok": True})


async def _handle_dispatch_punchlist_item(receive, send, component_id, item_id):
    """Dispatch a punchlist item as a new task."""
    items = await db.list_punchlist(component_id)
    item = next((i for i in items if i["id"] == item_id), None)
    if not item:
        return await _error(send, f"Punchlist item {item_id} not found", 404)

    component = await db.get_component(component_id)
    if not component:
        return await _error(send, f"Component '{component_id}' not found", 404)

    body = await _read_body(receive)
    extra = json.loads(body) if body else {}

    # Generate a task_id slug from the punchlist item
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", item["item"].lower()).strip("-")[:40]
    project_id = component["project_id"]
    new_task_id = f"{project_id}/punchlist-{item_id}-{slug}"

    result = await tasks.dispatch_task(
        project_id=project_id,
        task_id=new_task_id,
        goal=item["item"],
        model=extra.get("model", "sonnet"),
    )
    # Set component_id on the new task
    await db.update_task(new_task_id, component_id=component_id)
    # Mark punchlist item as claimed
    await db.update_punchlist_item(item_id, status="claimed", claimed_by=new_task_id)
    await _json_response(send, {"task_id": new_task_id}, 201)


# ── Push subscriptions ──────────────────────────────────────────────────────

async def _handle_push_subscribe(receive, send):
    body = await _read_body(receive)
    if not body:
        return await _error(send, "Request body required")
    data = json.loads(body)
    endpoint = data.get("endpoint", "").strip()
    p256dh = data.get("p256dh", "").strip()
    auth = data.get("auth", "").strip()
    if not (endpoint and p256dh and auth):
        return await _error(send, "endpoint, p256dh, and auth are required")
    sub = await db.save_push_subscription(endpoint, p256dh, auth)
    await _json_response(send, sub, 201)


async def _handle_push_unsubscribe(receive, send):
    body = await _read_body(receive)
    if not body:
        return await _error(send, "Request body required")
    data = json.loads(body)
    endpoint = data.get("endpoint", "").strip()
    if not endpoint:
        return await _error(send, "endpoint is required")
    deleted = await db.delete_push_subscription(endpoint)
    await _json_response(send, {"deleted": deleted})


async def _handle_vapid_public_key(send):
    key = web_push.VAPID_PUBLIC_KEY
    await _json_response(send, {"vapid_public_key": key})


# ── Notification settings ────────────────────────────────────────────────────

async def _handle_get_notification_settings(send):
    settings = await db.get_notification_settings()
    await _json_response(send, settings)


async def _handle_update_notification_settings(receive, send):
    body = await _read_body(receive)
    if not body:
        return await _error(send, "Request body required")
    data = json.loads(body)
    allowed = {"notify_failed", "notify_needs_review", "notify_completed", "notify_question"}
    updates = {k: bool(v) for k, v in data.items() if k in allowed}
    settings = await db.update_notification_settings(**updates)
    await _json_response(send, settings)


# ── Settings helpers ──────────────────────────────────────────────────────────

def _is_admin(user: dict) -> bool:
    return user.get("role") in ("owner", "admin")


# ── Instance settings ─────────────────────────────────────────────────────────

async def _handle_get_instance_settings(scope, send):
    user = scope.get("session_user") or {}
    if not _is_admin(user):
        return await _error(send, "Forbidden", 403)

    instance = await db.get_instance()
    if not instance:
        return await _error(send, "Instance not found", 404)

    # GitHub connection status
    github_info = {"connected": False}
    try:
        pat = await db.get_instance_github_pat()
        # PAT exists — always expose last4 regardless of GitHub reachability
        github_info["pat_last4"] = pat[-4:]
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    "https://api.github.com/user",
                    headers={"Authorization": f"Bearer {pat}"},
                )
                if resp.status_code == 200:
                    gh_data = resp.json()
                    github_info["connected"] = True
                    github_info["username"] = gh_data.get("login")
        except Exception:
            pass  # PAT exists but GitHub unreachable — connected stays False
    except Exception:
        pass  # No PAT configured

    # OAuth client secret
    oauth_info = {}
    oauth_client = await _get_oauth_client("claude-mcp")
    if oauth_client:
        raw_secret = oauth_client.get("client_secret_encrypted")
        if raw_secret and is_fernet_token(raw_secret):
            decrypted_secret = decrypt_value(raw_secret)
        else:
            decrypted_secret = raw_secret
        oauth_info = {
            "client_id": oauth_client["client_id"],
            "client_secret": decrypted_secret,
        }

    await _json_response(send, {
        "instance": {
            "name": instance["name"],
            "slug": instance["slug"],
            "plan_tier": instance.get("plan_tier"),
        },
        "github": github_info,
        "oauth": oauth_info,
    })


async def _handle_patch_instance_settings(receive, send, scope):
    user = scope.get("session_user") or {}
    if not _is_admin(user):
        return await _error(send, "Forbidden", 403)

    body = await _read_body(receive)
    data = json.loads(body) if body else {}

    if "github_pat" in data:
        await db.set_instance_github_pat(data["github_pat"])

    await _json_response(send, {"ok": True})


async def _handle_test_github(send, scope):
    user = scope.get("session_user") or {}
    if not _is_admin(user):
        return await _error(send, "Forbidden", 403)

    try:
        pat = await db.get_instance_github_pat()
    except ValueError as e:
        return await _json_response(send, {"valid": False, "error": str(e)})

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.github.com/user",
                headers={"Authorization": f"Bearer {pat}"},
            )
            if resp.status_code == 200:
                gh_data = resp.json()
                return await _json_response(send, {"valid": True, "username": gh_data.get("login")})
            return await _json_response(send, {"valid": False, "error": f"GitHub returned {resp.status_code}"})
    except Exception as e:
        return await _json_response(send, {"valid": False, "error": str(e)})


async def _handle_regenerate_oauth_secret(send, scope):
    user = scope.get("session_user") or {}
    if not _is_admin(user):
        return await _error(send, "Forbidden", 403)

    new_secret = secrets.token_urlsafe(32)
    encrypted = encrypt_value(new_secret)

    async with db.get_db() as conn:
        cursor = await conn.execute(
            "UPDATE oauth_clients SET client_secret_encrypted = ? WHERE client_id = ?",
            (encrypted, "claude-mcp"),
        )
        await conn.commit()
        if cursor.rowcount == 0:
            return await _error(send, "OAuth client not found", 404)

    await _json_response(send, {"client_id": "claude-mcp", "client_secret": new_secret})


# ── User settings ─────────────────────────────────────────────────────────────

async def _handle_get_user_settings(scope, send):
    user = scope.get("session_user") or {}
    user_id = user.get("id")
    if not user_id:
        return await _error(send, "Not authenticated", 401)

    full_user = await db.get_user(user_id)
    if not full_user:
        return await _error(send, "User not found", 404)

    creds = await db.get_user_credentials(user_id) or {}
    ant_key = creds.get("anthropic_api_key")
    anthropic_info = {
        "configured": bool(ant_key),
        "key_last4": ant_key[-4:] if ant_key else None,
    }
    notif_prefs = creds.get("notification_preferences") or {}

    await _json_response(send, {
        "profile": {
            "name": full_user.get("name"),
            "email": full_user.get("email"),
            "timezone": full_user.get("timezone"),
            "role": full_user.get("role"),
        },
        "anthropic": anthropic_info,
        "notifications": notif_prefs,
    })


async def _handle_patch_user_settings(receive, send, scope):
    user = scope.get("session_user") or {}
    user_id = user.get("id")
    if not user_id:
        return await _error(send, "Not authenticated", 401)

    body = await _read_body(receive)
    data = json.loads(body) if body else {}

    # Update users table fields
    user_updates = {k: data[k] for k in ("name", "email", "timezone") if k in data}
    if user_updates:
        await db.update_user(user_id, **user_updates)

    # Update credentials
    cred_updates = {}
    if "anthropic_api_key" in data:
        cred_updates["anthropic_api_key"] = data["anthropic_api_key"]
    if "notification_preferences" in data:
        cred_updates["notification_preferences"] = data["notification_preferences"]
    if cred_updates:
        await db.update_user_credentials(user_id, **cred_updates)

    await _json_response(send, {"ok": True})


async def _handle_test_anthropic(send, scope):
    user = scope.get("session_user") or {}
    user_id = user.get("id")
    if not user_id:
        return await _error(send, "Not authenticated", 401)

    try:
        key = await db.get_anthropic_key(user_id)
    except ValueError as e:
        return await _json_response(send, {"valid": False, "error": str(e)})

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                },
            )
            if resp.status_code == 200:
                return await _json_response(send, {"valid": True})
            return await _json_response(send, {"valid": False, "error": f"Anthropic returned {resp.status_code}"})
    except Exception as e:
        return await _json_response(send, {"valid": False, "error": str(e)})


async def _handle_change_password(receive, send, scope):
    user = scope.get("session_user") or {}
    user_id = user.get("id")
    user_email = user.get("email")
    if not user_id or not user_email:
        return await _error(send, "Not authenticated", 401)

    body = await _read_body(receive)
    data = json.loads(body) if body else {}

    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")
    if not current_password or not new_password:
        return await _error(send, "current_password and new_password are required")

    full_user = await db.get_user_by_email_with_auth(user_email)
    if not full_user:
        return await _error(send, "User not found", 404)

    if not full_user.get("password_hash"):
        return await _error(send, "No password set for this account", 400)

    ph = PasswordHasher()
    try:
        ph.verify(full_user["password_hash"], current_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return await _error(send, "Current password is incorrect", 401)

    new_hash = ph.hash(new_password)
    await db.update_user(user_id, password_hash=new_hash)
    await _json_response(send, {"ok": True})


# ── File upload constants ──────────────────────────────────────────────────

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

ALLOWED_EXTENSIONS = {
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg',  # images
    'txt', 'md', 'json', 'csv', 'yaml', 'yml', 'toml', 'xml',  # text
    'pdf',  # documents
}

MIME_TYPES = {
    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
    'gif': 'image/gif', 'webp': 'image/webp', 'svg': 'image/svg+xml',
    'txt': 'text/plain', 'md': 'text/markdown', 'json': 'application/json',
    'csv': 'text/csv', 'yaml': 'application/yaml', 'yml': 'application/yaml',
    'toml': 'application/toml', 'xml': 'application/xml',
    'pdf': 'application/pdf',
}


def _get_header(scope, name: bytes) -> bytes | None:
    for key, val in scope.get("headers", []):
        if key.lower() == name.lower():
            return val
    return None


def _parse_multipart(body: bytes, boundary: bytes) -> tuple[str | None, bytes | None]:
    """Parse a multipart body and return (filename, file_data) from the first file field."""
    from python_multipart.multipart import MultipartParser, parse_options_header

    filename_holder = [None]
    data_chunks = []
    in_file = [False]
    current_header_field = [b""]
    current_header_value = [b""]
    headers = [{}]

    def on_header_field(data, start, end):
        current_header_field[0] += data[start:end]

    def on_header_value(data, start, end):
        current_header_value[0] += data[start:end]

    def on_header_end():
        field = current_header_field[0].lower()
        value = current_header_value[0]
        headers[0][field] = value
        current_header_field[0] = b""
        current_header_value[0] = b""

    def on_headers_finished():
        cd = headers[0].get(b"content-disposition", b"")
        _, params = parse_options_header(cd)
        fname = params.get(b"filename")
        if fname is not None:
            filename_holder[0] = fname.decode("utf-8", errors="replace")
            in_file[0] = True
        else:
            in_file[0] = False

    def on_part_data(data, start, end):
        if in_file[0]:
            data_chunks.append(bytes(data[start:end]))

    def on_part_end():
        in_file[0] = False
        headers[0] = {}

    callbacks = {
        "on_header_field": on_header_field,
        "on_header_value": on_header_value,
        "on_header_end": on_header_end,
        "on_headers_finished": on_headers_finished,
        "on_part_data": on_part_data,
        "on_part_end": on_part_end,
    }

    parser = MultipartParser(boundary, callbacks=callbacks)
    parser.write(body)
    parser.finalize()

    if filename_holder[0] is None:
        return None, None

    return filename_holder[0], b"".join(data_chunks)


# ── File handlers ──────────────────────────────────────────────────────────

async def _handle_list_files(send):
    files = await db.list_files()
    await _json_response(send, files)


async def _handle_upload_file(scope, receive, send):
    user = scope.get("session_user") or {}
    user_id = user.get("id")
    if not user_id:
        return await _error(send, "Not authenticated", 401)

    # Check Content-Length header for early rejection
    cl_header = _get_header(scope, b"content-length")
    if cl_header:
        try:
            cl = int(cl_header)
            if cl > MAX_FILE_SIZE:
                return await _error(send, "File too large. Maximum 10MB.", 413)
        except ValueError:
            pass

    # Parse content-type for boundary
    ct_header = _get_header(scope, b"content-type") or b""
    ct_str = ct_header.decode("latin-1", errors="replace")
    if "multipart/form-data" not in ct_str:
        return await _error(send, "Expected multipart/form-data", 400)

    from python_multipart.multipart import parse_options_header
    _, params = parse_options_header(ct_header)
    boundary = params.get(b"boundary")
    if not boundary:
        return await _error(send, "Missing multipart boundary", 400)

    body = await _read_body(receive)

    filename, file_data = _parse_multipart(body, boundary)
    if filename is None or file_data is None:
        return await _error(send, "No file found in request", 400)

    # Check actual file data size
    if len(file_data) > MAX_FILE_SIZE:
        return await _error(send, "File too large. Maximum 10MB.", 413)

    # Validate extension
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return await _error(
            send,
            f"File type .{ext} not allowed. Supported: images, text, PDF.",
            400,
        )

    # Save to disk
    file_id = str(uuid.uuid4())
    uploads_dir = Path.home() / "uploads" / file_id
    uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = uploads_dir / filename
    dest.write_bytes(file_data)

    mime_type = MIME_TYPES.get(ext)
    record = await db.create_file(
        id=file_id,
        filename=filename,
        stored_path=str(dest),
        mime_type=mime_type,
        size_bytes=len(file_data),
        uploaded_by=user_id,
    )
    await _json_response(send, record, status=201)


async def _handle_rename_file(receive, send, file_id: str, scope):
    user = scope.get("session_user") or {}
    if not user.get("id"):
        return await _error(send, "Not authenticated", 401)

    record = await db.get_file(file_id)
    if not record:
        return await _error(send, f"File '{file_id}' not found", 404)

    body = await _read_body(receive)
    data = json.loads(body) if body else {}
    new_name = data.get("filename", "").strip()
    if not new_name:
        return await _error(send, "filename is required")

    # Validate extension of new name
    ext = new_name.rsplit(".", 1)[-1].lower() if "." in new_name else ""
    if ext not in ALLOWED_EXTENSIONS:
        return await _error(
            send,
            f"File type .{ext} not allowed. Supported: images, text, PDF.",
            400,
        )

    # Rename on disk (within same UUID directory)
    old_path = Path(record["stored_path"])
    new_path = old_path.parent / new_name
    if old_path != new_path:
        old_path.rename(new_path)

    updated = await db.update_file(file_id, new_name, str(new_path))
    await _json_response(send, updated)


async def _handle_delete_file(send, file_id: str, scope):
    user = scope.get("session_user") or {}
    if not user.get("id"):
        return await _error(send, "Not authenticated", 401)

    record = await db.get_file(file_id)
    if not record:
        return await _error(send, f"File '{file_id}' not found", 404)

    # Remove UUID directory from disk
    stored = Path(record["stored_path"])
    uuid_dir = stored.parent
    if uuid_dir.exists() and uuid_dir.parent == (Path.home() / "uploads"):
        shutil.rmtree(uuid_dir, ignore_errors=True)

    await db.delete_file(file_id)
    await _json_response(send, {"ok": True})
