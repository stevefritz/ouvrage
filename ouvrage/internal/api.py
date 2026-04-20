"""Internal API — machine-to-machine endpoints for control plane integration.

Routes:
  POST /internal/config          — push concurrency_limit / max_projects
  POST /internal/bootstrap-user  — idempotent user creation
  GET  /internal/usage           — usage stats for the dashboard

Auth: Bearer token compared against INTERNAL_API_TOKEN env var.
Only active when AUTH_MODE=saas. All routes return 404 in local mode.
"""

import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone

import ouvrage.db as db
from ouvrage.config.settings import AUTH_MODE, INTERNAL_API_TOKEN

logger = logging.getLogger("ouvrage.internal.api")

_ALLOWED_CONFIG_FIELDS = frozenset({"concurrency_limit", "max_projects", "trial_ends_at"})


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _read_json_body(receive) -> tuple[dict | None, str | None]:
    """Read and parse the request body as JSON.

    Returns (data, error_message). On error, data is None.
    """
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    if not body:
        return {}, None
    try:
        return json.loads(body), None
    except (json.JSONDecodeError, ValueError) as e:
        return None, f"Invalid JSON: {e}"


async def _send_json(send, status: int, body: dict) -> None:
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [[b"content-type", b"application/json"]],
    })
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


async def _send_404(send) -> None:
    await send({"type": "http.response.start", "status": 404,
                "headers": [[b"content-type", b"text/plain"]]})
    await send({"type": "http.response.body", "body": b"Not Found"})


def _check_auth(scope) -> bool:
    """Return True if the request carries a valid INTERNAL_API_TOKEN Bearer."""
    if not INTERNAL_API_TOKEN:
        return False
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            auth_str = value.decode("utf-8", errors="replace")
            if auth_str.lower().startswith("bearer "):
                token = auth_str[7:].strip()
                return secrets.compare_digest(token, INTERNAL_API_TOKEN)
    return False


# ── Endpoint handlers ────────────────────────────────────────────────────────

async def _handle_config(scope, receive, send) -> None:
    """POST /internal/config"""
    data, err = await _read_json_body(receive)
    if err:
        await _send_json(send, 400, {"error": "invalid_json", "message": err})
        return

    # Reject unknown fields
    unknown = set(data.keys()) - _ALLOWED_CONFIG_FIELDS
    if unknown:
        await _send_json(send, 422, {
            "error": "unknown_fields",
            "message": f"Unknown fields: {sorted(unknown)}. Accepted: concurrency_limit, max_projects",
        })
        return

    # Validate field types
    concurrency_limit = data.get("concurrency_limit")
    max_projects = data.get("max_projects")
    trial_ends_at = data.get("trial_ends_at")

    for field_name, val in [("concurrency_limit", concurrency_limit), ("max_projects", max_projects)]:
        if val is not None and (not isinstance(val, int) or isinstance(val, bool)):
            await _send_json(send, 422, {
                "error": "invalid_type",
                "message": f"{field_name} must be an integer",
            })
            return

    if trial_ends_at is not None and not isinstance(trial_ends_at, str):
        await _send_json(send, 422, {
            "error": "invalid_type",
            "message": "trial_ends_at must be an ISO 8601 datetime string or null",
        })
        return

    cfg = await db.set_instance_config(
        concurrency_limit=concurrency_limit,
        max_projects=max_projects,
        trial_ends_at=trial_ends_at,
    )
    await _send_json(send, 200, {
        "ok": True,
        "concurrency_limit": cfg["concurrency_limit"],
        "max_projects": cfg["max_projects"],
        "trial_ends_at": cfg["trial_ends_at"],
    })

    # When max_projects changes, trigger drain for project-limit-blocked tasks.
    # Fire-and-forget — response already sent.
    if max_projects is not None:
        from ouvrage.dispatch.queue import _drain_queue, _drain_project_limit_blocked
        asyncio.create_task(_drain_queue())
        asyncio.create_task(_drain_project_limit_blocked())


async def _handle_bootstrap_user(scope, receive, send) -> None:
    """POST /internal/bootstrap-user"""
    data, err = await _read_json_body(receive)
    if err:
        await _send_json(send, 400, {"error": "invalid_json", "message": err})
        return

    email = data.get("email")
    role = data.get("role", "member")

    if not email:
        await _send_json(send, 422, {
            "error": "missing_field",
            "message": "email is required",
        })
        return

    if not isinstance(email, str):
        await _send_json(send, 422, {
            "error": "invalid_type",
            "message": "email must be a string",
        })
        return

    email = email.strip().lower()

    existing = await db.get_user_by_email(email)
    if existing:
        await _send_json(send, 200, {"ok": True, "created": False})
        return

    name = email.split("@")[0]
    try:
        user = await db.create_user(email=email, name=name, role=role)
    except Exception as e:
        logger.error("bootstrap-user create_user failed: %s", e)
        await _send_json(send, 500, {"error": "server_error", "message": "Internal error"})
        return

    # If instance has no owner yet (SaaS mode), set this user as the owner
    user_id = user.get("id") if isinstance(user, dict) else None
    if user_id:
        try:
            instance = await db.get_instance()
            if instance and not instance.get("owner_user_id"):
                await db.update_instance(owner_user_id=user_id)
                logger.info("Set instance owner to bootstrapped user %s (%s)", user_id, email)
        except Exception as e:
            logger.warning("Failed to set instance owner: %s", e)

    await _send_json(send, 200, {"ok": True, "created": True})


async def _handle_usage(scope, receive, send) -> None:
    """GET /internal/usage"""
    # Drain receive (GET has no body, but must drain the ASGI message)
    while True:
        message = await receive()
        if not message.get("more_body"):
            break

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start_iso = month_start.strftime("%Y-%m-%dT%H:%M:%SZ")

    from ouvrage.db.connection import get_db
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT
                COUNT(*)                                           AS tasks_this_month,
                COALESCE(SUM(total_cost_usd), 0.0)                AS total_cost_usd,
                SUM(CASE WHEN status = 'working' THEN 1 ELSE 0 END) AS active_tasks
            FROM tasks
            WHERE created_at >= ?
            """,
            (month_start_iso,),
        )
        row = rows[0]
        proj_rows = await conn.execute_fetchall("SELECT COUNT(*) AS projects_count FROM projects")
        projects_count = proj_rows[0]["projects_count"] or 0

    tasks_this_month = row["tasks_this_month"] or 0
    total_cost_usd = round(float(row["total_cost_usd"] or 0.0), 6)
    active_tasks = row["active_tasks"] or 0

    await _send_json(send, 200, {
        "tasks_this_month": tasks_this_month,
        "total_cost_usd": total_cost_usd,
        "active_tasks": active_tasks,
        "current_concurrency": active_tasks,
        "project_count": projects_count,
    })


# ── Main entry point ─────────────────────────────────────────────────────────

async def handle_request(scope, receive, send) -> None:
    """Route /internal/* requests.

    Returns 404 when AUTH_MODE != 'saas'.
    Returns 401 when Bearer token is missing or wrong.
    """
    if AUTH_MODE != "saas":
        await _send_404(send)
        return

    if not _check_auth(scope):
        await _send_json(send, 401, {"error": "unauthorized", "message": "Invalid or missing token"})
        return

    path = scope["path"]
    method = scope.get("method", "")

    if path == "/internal/config" and method == "POST":
        await _handle_config(scope, receive, send)
    elif path == "/internal/bootstrap-user" and method == "POST":
        await _handle_bootstrap_user(scope, receive, send)
    elif path == "/internal/usage" and method == "GET":
        await _handle_usage(scope, receive, send)
    else:
        await _send_404(send)
