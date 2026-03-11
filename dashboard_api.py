"""Dashboard REST API — JSON endpoints for the Switchboard SPA."""

import json
import time

import database as db
import tasks

_start_time = time.monotonic()


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
    params = {}
    for part in qs.split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            params[k] = v
    return params


def _extract_task_id(path: str, prefix: str) -> str:
    """Extract task_id from path after prefix. Handles slashes in IDs."""
    rest = path[len(prefix):]
    # Strip trailing action segments like /cancel, /retry, /resume, /messages, /session-log, /dispatch-log
    for suffix in ("/cancel", "/retry", "/resume", "/messages", "/session-log", "/dispatch-log"):
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
    })


async def _handle_list_projects(send):
    projects = await db.list_projects()
    # Enrich with task counts
    for p in projects:
        all_tasks = await db.list_tasks(project_id=p["id"])
        p["active_task_count"] = sum(1 for t in all_tasks if t["status"] == "working")
        p["total_tasks"] = len(all_tasks)
        p["total_cost"] = round(sum(t.get("total_cost_usd", 0) or 0 for t in all_tasks), 2)
    await _json_response(send, projects)


async def _handle_get_project(send, project_id):
    project = await db.get_project(project_id)
    if not project:
        return await _error(send, f"Project '{project_id}' not found", 404)
    task_list = await db.list_tasks(project_id=project_id)
    project["tasks"] = task_list
    await _json_response(send, project)


async def _handle_list_tasks(scope, send):
    params = _parse_qs(scope)
    task_list = await db.list_tasks(
        project_id=params.get("project_id"),
        status=params.get("status"),
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
