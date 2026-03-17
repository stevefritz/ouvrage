"""Dashboard REST API — JSON endpoints for the Switchboard SPA."""

import json
import os
import time
from urllib.parse import parse_qs, unquote

import database as db
import tasks

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
                    "/advance-chain", "/cancel-chain", "/chain", "/review-task",
                    "/messages", "/session-log", "/dispatch-log"):
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

        # GET /dashboard/api/projects/{id}
        if path.startswith("/dashboard/api/projects/") and method == "GET":
            project_id = path[len("/dashboard/api/projects/"):]
            return await _handle_get_project(send, project_id)

        # GET /dashboard/api/conversations
        if path == "/dashboard/api/conversations" and method == "GET":
            return await _handle_list_conversations(scope, send)

        # GET /dashboard/api/conversations/{id}
        if path.startswith("/dashboard/api/conversations/") and method == "GET":
            conv_id = unquote(path[len("/dashboard/api/conversations/"):])
            return await _handle_get_conversation(scope, send, conv_id)

        # GET /dashboard/api/activity
        if path == "/dashboard/api/activity" and method == "GET":
            return await _handle_activity(scope, send)

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
                    return await _handle_session_log(send, task_id)
                if rest.endswith("/dispatch-log"):
                    task_id = rest[:-len("/dispatch-log")]
                    return await _handle_dispatch_log(send, task_id)
                if rest.endswith("/chain"):
                    task_id = rest[:-len("/chain")]
                    return await _handle_get_chain(send, task_id)
                if rest.endswith("/review-task"):
                    task_id = rest[:-len("/review-task")]
                    return await _handle_get_review_task(send, task_id)

                # GET /dashboard/api/tasks/{task_id} (detail)
                return await _handle_get_task(send, rest)

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
        "max_concurrent": db.DEFAULT_MAX_CONCURRENT,
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
    limit = min(int(params.get("limit", 30)), 100)
    offset = int(params.get("offset", 0))
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

    await _json_response(send, task)


async def _handle_get_messages(scope, send, task_id):
    params = _parse_qs(scope)
    last_n = int(params["limit"]) if "limit" in params else None
    after = int(params["after"]) if "after" in params else None
    thread = await db.read_task_messages(task_id, last_n=last_n, after=after)
    await _json_response(send, thread)


async def _handle_session_log(send, task_id):
    task = await db.get_task(task_id)
    if not task:
        return await _error(send, f"Task '{task_id}' not found", 404)

    wt = task.get("worktree_path")
    if not wt:
        return await _json_response(send, [])

    import os
    log_path = os.path.join(wt, ".switchboard", "session.jsonl")
    if not os.path.exists(log_path):
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


async def _handle_dispatch_log(send, task_id):
    task = await db.get_task(task_id)
    if not task:
        return await _error(send, f"Task '{task_id}' not found", 404)

    wt = task.get("worktree_path")
    if not wt:
        return await _text_response(send, "")

    import os
    log_path = os.path.join(wt, ".switchboard", "dispatch.log")
    if not os.path.exists(log_path):
        return await _text_response(send, "")

    try:
        with open(log_path) as f:
            return await _text_response(send, f.read())
    except Exception:
        return await _text_response(send, "")


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


async def _handle_close(receive, send, task_id):
    body = await _read_body(receive)
    data = json.loads(body) if body else {}
    result = await tasks.close_task(
        task_id=task_id,
        cleanup=data.get("cleanup", True),
        force_delete_branch=data.get("force_delete_branch", False),
    )
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
