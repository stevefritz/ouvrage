"""Dashboard REST API — JSON endpoints for the Switchboard SPA."""

import asyncio
import json
import logging
import os
import secrets
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

import switchboard.db as db
import switchboard.dispatch as tasks
from switchboard.auth.oauth import get_client as _get_oauth_client
from switchboard.config.constants import DEFAULT_MAX_CONCURRENT
from switchboard.config import settings as _settings
from switchboard.crypto import decrypt_value, encrypt_value, is_fernet_token
from switchboard.git.operations import normalize_repo_url
from switchboard.notifications import web_push
from switchboard.server.context import get_request_user_id

_WORKTREE_BASE = os.environ.get("WORKTREE_BASE", "/work")

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
                    "/test-output", "/gate-session-log", "/stop", "/actions"):
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

        # POST /dashboard/api/projects
        if path == "/dashboard/api/projects" and method == "POST":
            return await _handle_create_project(receive, send, scope)

        # /dashboard/api/projects/{id}[/pause|resume|stop]
        if path.startswith("/dashboard/api/projects/"):
            rest = path[len("/dashboard/api/projects/"):]
            if method == "GET" and "/" not in rest:
                return await _handle_get_project(send, rest)
            if method == "PATCH" and "/" not in rest:
                return await _handle_update_project(receive, send, rest)
            if method == "DELETE" and "/" not in rest:
                return await _handle_delete_project(send, rest, scope)
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

        # GET /dashboard/api/conversations
        if path == "/dashboard/api/conversations" and method == "GET":
            return await _handle_list_conversations(scope, send)

        # GET/POST /dashboard/api/conversations/{id}[/messages|/search]
        if path.startswith("/dashboard/api/conversations/"):
            rest = path[len("/dashboard/api/conversations/"):]
            if method == "POST" and rest.endswith("/messages"):
                conv_id = unquote(rest[:-len("/messages")])
                return await _handle_post_conversation_message(receive, send, conv_id)
            if method == "GET" and rest.endswith("/search"):
                conv_id = unquote(rest[:-len("/search")])
                return await _handle_search_conversation(scope, send, conv_id)
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

        # POST /dashboard/api/tasks — create a new task
        if path == "/dashboard/api/tasks" and method == "POST":
            return await _handle_create_task(scope, receive, send)

        # GET /dashboard/api/tasks/depends-on-candidates
        if path == "/dashboard/api/tasks/depends-on-candidates" and method == "GET":
            return await _handle_depends_on_candidates(scope, send)

        # Task-specific routes: /dashboard/api/tasks/{task_id}[/action]
        if path.startswith("/dashboard/api/tasks/"):
            rest = path[len("/dashboard/api/tasks/"):]

            # POST actions
            if method == "POST":
                if rest.endswith("/stop"):
                    task_id = rest[:-len("/stop")]
                    return await _handle_stop(send, task_id)
                if rest.endswith("/cancel"):
                    task_id = rest[:-len("/cancel")]
                    return await _handle_cancel(send, task_id)
                if rest.endswith("/retry"):
                    task_id = rest[:-len("/retry")]
                    return await _handle_retry(receive, send, task_id)
                if rest.endswith("/resume"):
                    task_id = rest[:-len("/resume")]
                    return await _handle_resume(receive, send, task_id)
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
                    return await _handle_dispatch(scope, send, task_id)
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

            # PATCH /dashboard/api/tasks/{task_id} — update mutable metadata
            if method == "PATCH":
                return await _handle_update_task(receive, send, rest)

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
                if rest.endswith("/files"):
                    task_id = rest[:-len("/files")]
                    return await _handle_task_files(send, task_id)
                if rest.endswith("/actions"):
                    task_id = _extract_task_id(path, "/dashboard/api/tasks/")
                    return await _handle_get_actions(send, task_id)

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

        # Git credential settings (owner/admin only)
        if path == "/dashboard/api/settings/git-credentials" and method == "GET":
            return await _handle_get_git_credentials(send, scope)
        if path.startswith("/dashboard/api/settings/git-credentials/"):
            rest = path[len("/dashboard/api/settings/git-credentials/"):]
            if rest.endswith("/test") and method == "POST":
                provider = rest[:-len("/test")]
                return await _handle_test_git_credential(send, scope, provider)
            if method == "PUT":
                return await _handle_put_git_credential(receive, send, scope, rest)
            if method == "DELETE":
                return await _handle_delete_git_credential(send, scope, rest)

        # User settings (each user sees their own)
        if path == "/dashboard/api/settings/user" and method == "GET":
            return await _handle_get_user_settings(scope, send)
        if path == "/dashboard/api/settings/user" and method == "PATCH":
            return await _handle_patch_user_settings(receive, send, scope)
        if path == "/dashboard/api/settings/user/test-anthropic" and method == "POST":
            return await _handle_test_anthropic(send, scope)
        if path == "/dashboard/api/settings/user/change-password" and method == "POST":
            return await _handle_change_password(receive, send, scope)

        # API token management
        if path == "/dashboard/api/settings/tokens" and method == "GET":
            return await _handle_list_tokens(scope, send)
        if path == "/dashboard/api/settings/tokens" and method == "POST":
            return await _handle_create_token(receive, scope, send)
        if path.startswith("/dashboard/api/settings/tokens/") and method == "DELETE":
            token_id = path[len("/dashboard/api/settings/tokens/"):]
            return await _handle_revoke_token(scope, send, token_id)

        # ── Runtime info endpoint ──────────────────────────────────────────
        if path == "/dashboard/api/runtime-info" and method == "GET":
            return await _handle_runtime_info(send)

        # ── Search endpoint ────────────────────────────────────────────────
        if path == "/dashboard/api/search" and method == "GET":
            return await _handle_search_api(scope, send)

        # ── Files endpoints ────────────────────────────────────────────────
        if path == "/dashboard/api/files" and method == "GET":
            return await _handle_list_files(scope, send)
        if path == "/dashboard/api/files" and method == "POST":
            return await _handle_upload_file(scope, receive, send)
        if path.startswith("/dashboard/api/files/"):
            file_id = path[len("/dashboard/api/files/"):]
            if file_id.endswith("/download") and method == "GET":
                actual_id = file_id[:-len("/download")]
                return await _handle_download_file(send, actual_id, scope)
            if file_id.endswith("/promote") and method == "POST":
                actual_id = file_id[:-len("/promote")]
                return await _handle_promote_file(receive, send, actual_id, scope)
            if method == "GET":
                return await _handle_get_file(send, file_id, scope)
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
        logger.exception("Unhandled exception in dashboard API: %s %s", method, path)
        await _error(send, f"Internal error: {e}", 500)


# ── Handlers ──────────────────────────────────────────────────────────────

async def _handle_system(send):
    active = await db.count_active_tasks()
    projects = await db.list_projects()
    all_tasks = await db.list_tasks()
    total_cost = sum(t.get("total_cost_usd", 0) or 0 for t in all_tasks)
    instance_cfg = await db.get_instance_config()
    await _json_response(send, {
        "active_tasks": active,
        "max_concurrent": DEFAULT_MAX_CONCURRENT,
        "total_cost_usd": round(total_cost, 2),
        "uptime_seconds": round(time.monotonic() - _start_time),
        "jira_base_url": JIRA_BASE_URL or None,
        "trial_ends_at": instance_cfg.get("trial_ends_at"),
    })


# Runtime commands: (key, display_name, args, pkg_manager)
_RUNTIME_COMMANDS = [
    ("python",     "Python",    ["python3", "--version"],           "pip"),
    ("node",       "Node.js",   ["node", "--version"],              "npm"),
    ("typescript", "TypeScript",["tsc", "--version"],               "tsc"),
    ("php",        "PHP",       ["php", "--version"],               "Composer"),
    ("ruby",       "Ruby",      ["ruby", "--version"],              "Bundler"),
    ("go",         "Go",        ["go", "version"],                  None),
    ("rust",       "Rust",      ["rustc", "--version"],             "Cargo"),
    ("java",       "Java",      ["java", "-version"],               "Maven, Gradle"),
    ("dotnet",     "C# / .NET", ["dotnet", "--version"],            "dotnet CLI"),
]


async def _run_version_cmd(args: list[str]) -> str:
    """Run a version command and return its stdout+stderr, or empty string on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        output = (stdout or stderr or b"").decode("utf-8", errors="replace").strip()
        return output
    except Exception:
        return ""


def _parse_version(key: str, raw: str) -> str:
    """Extract a clean version string from raw command output."""
    if not raw:
        return "not installed"
    # Most tools: first line, first token that looks like a version
    first_line = raw.splitlines()[0].strip()
    import re
    # Match patterns like "3.13.0", "v22.0.0", "1.23.4", "21.0.1", "9.0.100"
    m = re.search(r'v?(\d+[\d.]+)', first_line)
    if m:
        return m.group(0).lstrip("v")
    return first_line[:40]  # fallback: first 40 chars of first line


async def _handle_runtime_info(send):
    runtimes = []
    for key, name, args, pkg_manager in _RUNTIME_COMMANDS:
        raw = await _run_version_cmd(args)
        version = _parse_version(key, raw)
        runtimes.append({
            "key": key,
            "name": name,
            "version": version,
            "pkg_manager": pkg_manager,
        })
    await _json_response(send, runtimes)


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


async def _run_dashboard_project_validation(project_id: str, project: dict) -> dict:
    """Validate project credential access and store result. Returns updated project dict."""
    from switchboard.git.validation import validate_project_access

    try:
        result = await validate_project_access(project)
        updated = await db.update_project(
            project_id,
            credential_status=result["status"],
            credential_status_message=result["message"],
            credential_checked_at=result["checked_at"],
        )
        return updated
    except Exception as e:
        logger.warning("Credential validation failed for %s: %s", project_id, e)
        return project


async def _handle_create_project(receive, send, scope):
    body = await _read_body(receive)
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return await _error(send, "Invalid JSON body", 400)

    project_id = data.get("id", "").strip()
    repo_raw = data.get("repo", "").strip()

    if not project_id:
        return await _error(send, "id is required", 400)
    if not repo_raw:
        return await _error(send, "repo is required", 400)

    import re
    if not re.match(r'^[a-z0-9][a-z0-9-]*$', project_id):
        return await _error(send, "id must start with alphanumeric and contain only lowercase letters, numbers, and hyphens", 400)

    try:
        repo = normalize_repo_url(repo_raw)
    except Exception as exc:
        return await _error(send, f"Invalid repo URL: {exc}", 400)

    # Derive working_dir from repo URL
    folder_name = data.get("folder_name")
    if folder_name:
        name = folder_name
    else:
        name = repo.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        if ":" in name:
            name = name.rsplit(":", 1)[-1].removesuffix(".git")
    name = name.replace("/", "").replace("..", "").replace("\\", "")
    if not name:
        return await _error(send, "Could not derive folder name from repo URL", 400)
    working_dir = os.path.join(_WORKTREE_BASE, name)
    resolved = os.path.realpath(working_dir)
    base = os.path.realpath(_WORKTREE_BASE)
    if not resolved.startswith(base + "/") and resolved != base:
        return await _error(send, f"working_dir must be under {_WORKTREE_BASE}", 400)

    # Check for working_dir collision
    existing = await db.list_projects()
    for p in existing:
        if os.path.realpath(p["working_dir"]) == resolved:
            return await _error(send, f"working_dir '{resolved}' already belongs to project '{p['id']}' — use folder_name to override", 400)

    REQUIRED = ["model", "review_model", "auto_test", "auto_review", "auto_pr", "auto_merge", "max_turns", "max_wall_clock"]
    missing = [f for f in REQUIRED if data.get(f) is None]
    if missing:
        return await _error(send, f"Missing required config fields: {', '.join(missing)}", 400)

    try:
        # credential_override takes priority over legacy github_pat_override
        cred_raw = data.get("credential_override") or data.get("github_pat_override")
        cred_last4 = cred_raw[-4:] if cred_raw and len(cred_raw) >= 4 else None
        cred_encrypted = encrypt_value(cred_raw) if cred_raw and not is_fernet_token(cred_raw) else cred_raw or None

        result = await db.create_project(
            id=project_id,
            repo=repo,
            working_dir=resolved,
            default_branch=data.get("default_branch", "main"),
            setup_command=data.get("setup_command"),
            teardown_command=data.get("teardown_command"),
            test_command=data.get("test_command"),
            env_overrides=data.get("env_overrides"),
            max_turns=data.get("max_turns"),
            max_wall_clock=data.get("max_wall_clock"),
            model=data.get("model"),
            review_model=data.get("review_model"),
            review_ignore_patterns=data.get("review_ignore_patterns"),
            auto_test=data.get("auto_test"),
            auto_review=data.get("auto_review"),
            auto_pr=data.get("auto_pr"),
            auto_merge=data.get("auto_merge"),
            created_by=get_request_user_id(),
            provider=data.get("provider") or None,
            credential_override=cred_encrypted,
            credential_override_last4=cred_last4,
        )
        # Validate credential access synchronously so the response includes status
        result = await _run_dashboard_project_validation(project_id, result)

        await _json_response(send, result, 201)
    except Exception as exc:
        logger.exception("Error creating project '%s'", project_id)
        return await _error(send, str(exc), 500)


async def _handle_get_project(send, project_id):
    project = await db.get_project(project_id)
    if not project:
        return await _error(send, f"Project '{project_id}' not found", 404)
    task_list = await db.list_tasks(project_id=project_id)
    project["tasks"] = task_list
    # Strip encrypted credential values — frontend only needs last4
    project.pop("credential_override", None)
    project.pop("github_pat_override", None)
    await _json_response(send, project)


async def _handle_update_project(receive, send, project_id):
    """PATCH /dashboard/api/projects/{id} — update mutable project config fields."""
    body = await _read_body(receive)
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return await _error(send, "Invalid JSON body", 400)

    ALLOWED = {
        "display_name", "default_branch", "setup_command", "teardown_command", "test_command",
        "env_overrides", "max_turns", "max_wall_clock", "model", "review_model",
        "review_ignore_patterns", "auto_test", "auto_review", "auto_pr", "auto_merge",
        "state_definitions", "github_pat_override", "provider", "credential_override",
    }
    fields = {k: v for k, v in data.items() if k in ALLOWED}
    if not fields:
        project = await db.get_project(project_id)
        if not project:
            return await _error(send, f"Project '{project_id}' not found", 404)
        return await _json_response(send, project)

    if "github_pat_override" in fields:
        pat = fields["github_pat_override"]
        if pat:  # non-empty → encrypt
            fields["github_pat_override"] = encrypt_value(pat) if not is_fernet_token(pat) else pat
        else:  # empty string or null → clear
            fields["github_pat_override"] = "" if pat == "" else None

    if "credential_override" in fields:
        cred = fields["credential_override"]
        if cred:  # non-empty → encrypt
            fields["credential_override_last4"] = cred[-4:] if len(cred) >= 4 else cred
            fields["credential_override"] = encrypt_value(cred) if not is_fernet_token(cred) else cred
        else:  # empty string or null → clear
            fields["credential_override"] = "" if cred == "" else None
            fields["credential_override_last4"] = None

    try:
        result = await db.update_project(project_id, **fields)

        # Re-validate credential if repo, provider, or credential changed
        revalidate_keys = {"provider", "credential_override", "github_pat_override"}
        if revalidate_keys & fields.keys():
            result = await _run_dashboard_project_validation(project_id, result)

        await _json_response(send, result)
    except ValueError as exc:
        return await _error(send, str(exc), 404)
    except Exception as exc:
        return await _error(send, str(exc), 400)


async def _handle_delete_project(send, project_id, scope):
    """DELETE /dashboard/api/projects/{id} — delete project and its working directory."""
    user = scope.get("session_user") or {}
    if not user.get("id"):
        return await _error(send, "Not authenticated", 401)

    project = await db.get_project(project_id)
    if not project:
        return await _error(send, f"Project '{project_id}' not found", 404)

    # Reject if project has working tasks
    working_tasks = await db.list_tasks(project_id=project_id, status="working")
    if working_tasks:
        task_ids = [t["id"] for t in working_tasks]
        return await _error(
            send,
            f"Cannot delete project '{project_id}' — {len(working_tasks)} task(s) are still working: "
            f"{', '.join(task_ids)}. Cancel or wait for them to finish first.",
            409,
        )

    working_dir = project.get("working_dir")

    # Delete DB row
    try:
        await db.delete_project(project_id)
    except ValueError as e:
        return await _error(send, str(e), 404)

    # Remove working directory from disk
    if working_dir and os.path.isdir(working_dir):
        try:
            shutil.rmtree(working_dir)
        except Exception as e:
            logger.warning("Failed to remove working directory '%s' for project '%s': %s", working_dir, project_id, e)
            return await _json_response(send, {
                "deleted": True,
                "project_id": project_id,
                "warning": f"Project deleted but failed to remove working directory: {e}",
            })

    await _json_response(send, {"deleted": True, "project_id": project_id})


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


async def _handle_depends_on_candidates(scope, send):
    """GET /dashboard/api/tasks/depends-on-candidates?project_id=X"""
    params = _parse_qs(scope)
    project_id = params.get("project_id")
    if not project_id:
        return await _error(send, "project_id query parameter is required")

    # Get all tasks in this project
    all_tasks = await db.list_tasks(project_id=project_id, active_only=False)

    # Find task IDs that already have a dependent
    taken = set()
    for t in all_tasks:
        dep = t.get("depends_on")
        if dep:
            taken.add(dep.lower())

    # Filter: only tasks that don't already have a dependent
    candidates = []
    for t in all_tasks:
        if t["id"].lower() not in taken:
            candidates.append({
                "id": t["id"],
                "goal": t.get("goal", ""),
                "status": t.get("status", ""),
            })

    await _json_response(send, candidates)


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

    # Compute cache hit percentage
    total_input = task.get("total_input_tokens") or 0
    cache_read = task.get("total_cache_read_tokens") or 0
    task["cache_hit_pct"] = round((cache_read / total_input * 100), 1) if total_input > 0 else 0

    await _json_response(send, task)


async def _handle_update_task(receive, send, task_id):
    """PATCH /dashboard/api/tasks/{task_id} — update mutable task metadata."""
    task = await db.get_task(task_id)
    if not task:
        return await _error(send, f"Task '{task_id}' not found", 404)

    body = await _read_body(receive)
    data = json.loads(body) if body else {}
    if not data:
        return await _error(send, "No fields to update")

    # Validate depends_on if being updated
    if "depends_on" in data and data["depends_on"] is not None:
        try:
            data["depends_on"] = await tasks.validate_depends_on(
                data["depends_on"], task["project_id"], task_id
            )
        except ValueError as e:
            return await _error(send, str(e))

    try:
        result = await db.update_task(task_id, **data)
        await _json_response(send, result)
    except ValueError as e:
        await _error(send, str(e), 404)
    except Exception as e:
        logger.warning("update_task error for %s: %s", task_id, e)
        await _error(send, str(e), 400)


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


def _filter_empty_text_entries(entries):
    """Remove AssistantMessage entries that are purely empty/whitespace text (no tool_use)."""
    result = []
    for entry in entries:
        if entry.get("type") == "AssistantMessage":
            blocks = entry.get("content") or []
            has_tool_use = any(b.get("type") == "tool_use" for b in blocks)
            if not has_tool_use:
                text_blocks = [b for b in blocks if b.get("type") == "text"]
                if all(not (b.get("text") or "").strip() for b in text_blocks):
                    continue
        result.append(entry)
    return result


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

    await _json_response(send, _filter_empty_text_entries(entries))


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

    await _json_response(send, _filter_empty_text_entries(entries))


async def _handle_get_attempts(send, task_id):
    try:
        attempts = await db.get_task_attempts(task_id)
    except ValueError as e:
        return await _error(send, str(e), 404)
    await _json_response(send, {"task_id": task_id, "attempts": attempts})


async def _handle_get_actions(send, task_id):
    """GET /dashboard/api/tasks/{id}/actions — return available actions + state info."""
    from switchboard.dispatch.lifecycle import lifecycle
    task = await db.get_task(task_id)
    if not task:
        return await _error(send, f"Task '{task_id}' not found", 404)
    try:
        actions_raw = await lifecycle.get_available_actions(task_id)
        state_info = await lifecycle.get_state_label(task_id)
    except ValueError as e:
        return await _error(send, str(e), 404)
    # Convert underscore action names to hyphenated for frontend compatibility
    actions = []
    for a in actions_raw:
        action_dict = {
            "name": a["name"].replace("_", "-"),
            "label": a["label"],
            "style": a["style"],
            "confirm": a["confirm"],
        }
        if "options" in a:
            action_dict["options"] = a["options"]
        actions.append(action_dict)
    response = {
        "task_id": task_id,
        "state": {
            "status": state_info["state"],
            "reason": state_info["reason"],
            "label": state_info["label"],
            "color": state_info["color"],
            "pulse": state_info["pulse"],
            "queued_reason": state_info.get("queued_reason"),
            "queued_blocking_task_id": state_info.get("queued_blocking_task_id"),
        },
        "actions": actions,
    }
    await _json_response(send, response)


# ── Actions ───────────────────────────────────────────────────────────────

async def _handle_stop(send, task_id):
    result = await tasks.stop_task(task_id)
    await _json_response(send, result)


async def _handle_cancel(send, task_id):
    result = await tasks.cancel_task(task_id)
    await _json_response(send, result)


async def _handle_retry(receive, send, task_id):
    body = await _read_body(receive)
    data = json.loads(body) if body else {}
    result = await tasks.retry_task(task_id, clean=data.get("clean", False))
    await _json_response(send, result)


async def _handle_resume(receive, send, task_id):
    body = await _read_body(receive)
    params = json.loads(body) if body else {}
    auto_test = params.get("auto_test")
    auto_review = params.get("auto_review")
    result = await tasks.resume_task(
        task_id,
        auto_test=bool(auto_test) if auto_test is not None else None,
        auto_review=bool(auto_review) if auto_review is not None else None,
    )
    await _json_response(send, result)


async def _handle_approve(send, task_id):
    result = await tasks.approve_task(task_id)
    await _json_response(send, result)


async def _handle_create_task(scope, receive, send):
    """Create a new task via the dashboard form. Held by default."""
    body = await _read_body(receive)
    if not body:
        return await _error(send, "Request body required")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return await _error(send, "Invalid JSON")

    project_id = data.get("project_id", "").strip()
    task_id = data.get("id", "").strip()
    goal = data.get("goal", "").strip()

    if not project_id:
        return await _error(send, "project_id is required")
    if not task_id:
        return await _error(send, "id is required")
    if not goal:
        return await _error(send, "goal is required")

    # Check for duplicate task ID
    existing = await db.get_task(task_id)
    if existing:
        return await _error(send, f"Task '{task_id}' already exists", 409)

    user = scope.get("session_user") or {}
    user_id = user.get("id")

    try:
        result = await tasks.dispatch_task(
            project_id=project_id,
            task_id=task_id,
            goal=goal,
            spec=data.get("spec") or None,
            checklist=data.get("checklist") or None,
            held=data.get("held", True),
            model=data.get("model") or None,
            review_model=data.get("review_model") or None,
            auto_test=data.get("auto_test"),
            auto_review=data.get("auto_review"),
            auto_pr=data.get("auto_pr"),
            auto_merge=data.get("auto_merge"),
            max_turns=data.get("max_turns") or None,
            max_wall_clock=data.get("max_wall_clock") or None,
            max_test_retries=data.get("max_test_retries") or None,
            max_review_retries=data.get("max_review_retries") or None,
            component_id=data.get("component_id") or None,
            depends_on=data.get("depends_on") or None,
            base_branch=data.get("base_branch") or None,
            escalation_criteria=data.get("escalation_criteria") or None,
            created_by=user_id,
            dispatched_by=user_id,
        )
        # Store tags separately (dispatch_task doesn't accept tags param)
        tags = data.get("tags")
        if tags and isinstance(tags, list):
            await db.update_task(task_id, tags=tags)
        await _json_response(send, {"task_id": task_id, "project_id": project_id, **result}, 201)
    except ValueError as e:
        await _error(send, str(e))
    except Exception as e:
        logger.exception("Error creating task")
        await _error(send, str(e), 500)


async def _handle_dispatch(scope, send, task_id):
    task = await db.get_task(task_id)
    if not task:
        return await _error(send, f"Task '{task_id}' not found", 404)
    if task["status"] != "ready":
        return await _error(send, f"Task is '{task['status']}', expected 'ready'", 400)
    user = scope.get("session_user") or {}
    user_id = user.get("id")
    # Set dispatched_by if not already set (task may have been created by someone else)
    if user_id and not task.get("dispatched_by"):
        await db.update_task(task_id, dispatched_by=user_id)
    result = await tasks.dispatch_task(
        project_id=task["project_id"],
        task_id=task_id,
        goal=task["goal"],
    )
    await _json_response(send, result)


async def _handle_close(receive, send, task_id):
    await _read_body(receive)  # consume request body
    result = await tasks.close_task(task_id=task_id)
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


async def _handle_search_conversation(scope, send, conv_id: str):
    """GET /dashboard/api/conversations/{id}/search?q=...

    Runs LIKE search on message content within the conversation.
    If OPENAI_API_KEY is set, also runs semantic search and merges results.
    Returns message objects (id, author, type, title, snippet, score, created_at).
    """
    params = _parse_qs(scope)
    q = params.get("q", "").strip()
    if not q:
        return await _error(send, "Missing required query parameter: q", 400)

    # LIKE search — always runs
    results = await db.search_conversation_messages(conv_id, q)
    seen_ids = {r["id"] for r in results}

    # Semantic search — runs only when OpenAI API key is available
    from switchboard.embeddings.service import _get_openai_api_key
    if _get_openai_api_key():
        try:
            from switchboard.embeddings import service as emb
            from switchboard.embeddings.service import compute_relevance_score
            service = emb.get_embedding_service()
            query_vector = await service.embed_safe(q)
            if query_vector:
                semantic_hits = await db.search_messages_semantic(
                    query_vector=query_vector,
                    conversation_id=conv_id,
                    limit=20,
                )
                for hit in semantic_hits:
                    msg_id = hit["message_id"]
                    if msg_id not in seen_ids:
                        content = hit.get("content") or ""
                        snippet = content[:200] + ("..." if len(content) > 200 else "")
                        results.append({
                            "id": msg_id,
                            "author": hit.get("author"),
                            "type": hit.get("type"),
                            "title": hit.get("title"),
                            "snippet": snippet,
                            "score": round(compute_relevance_score(
                                hit["similarity"], hit.get("type"), hit.get("pinned", False)
                            ), 4),
                            "created_at": hit.get("created_at"),
                        })
                        seen_ids.add(msg_id)
        except Exception:
            pass  # Semantic search is best-effort; LIKE results still returned

    await _json_response(send, {"results": results, "total": len(results)})


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


# ── Git credential settings ───────────────────────────────────────────────────

_GIT_PROVIDERS = ("github", "gitlab", "bitbucket")
_GIT_DEFAULT_HOSTNAMES = {
    "github": "github.com",
    "gitlab": "gitlab.com",
    "bitbucket": "bitbucket.org",
}


async def _handle_get_git_credentials(send, scope):
    """GET /dashboard/api/settings/git-credentials — list all provider states."""
    user = scope.get("session_user") or {}
    if not _is_admin(user):
        return await _error(send, "Forbidden", 403)

    rows = await db.list_credentials()
    by_provider = {r["provider"]: r for r in rows}

    result = []
    for provider in _GIT_PROVIDERS:
        row = by_provider.get(provider)
        default_host = _GIT_DEFAULT_HOSTNAMES[provider]
        if row:
            hostname = row["hostname"] or default_host
            result.append({
                "provider": provider,
                "hostname": hostname,
                "hostname_is_default": hostname == default_host,
                "configured": True,
                "credential_last4": row.get("credential_last4"),
            })
        else:
            result.append({
                "provider": provider,
                "hostname": default_host,
                "hostname_is_default": True,
                "configured": False,
                "credential_last4": None,
            })

    await _json_response(send, {"credentials": result})


async def _check_credential_auth(provider: str, credential: str, hostname: str) -> dict:
    """Run auth check for a provider credential. Returns {ok, username, scopes, message}."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if provider == "github":
                api_base = f"https://{hostname}/api/v3" if hostname != "github.com" else "https://api.github.com"
                resp = await client.get(
                    f"{api_base}/user",
                    headers={"Authorization": f"Bearer {credential}"},
                )
                if resp.status_code in (401, 403):
                    return {"ok": False, "username": None, "scopes": None,
                            "message": "Authentication failed — token may be invalid or expired"}
                if resp.status_code != 200:
                    return {"ok": False, "username": None, "scopes": None,
                            "message": f"GitHub returned {resp.status_code}"}

                username = resp.json().get("login")
                oauth_scopes_header = resp.headers.get("X-OAuth-Scopes", "")
                if oauth_scopes_header:
                    scopes = [s.strip() for s in oauth_scopes_header.split(",") if s.strip()]
                    has_repo = "repo" in scopes
                    if has_repo:
                        message = f"Authenticated as {username}. Required scopes present."
                    else:
                        message = "Authenticated but token is missing 'repo' scope. Ouvrage requires full repository access."
                    return {"ok": has_repo, "username": username, "scopes": scopes, "message": message}
                else:
                    return {"ok": True, "username": username, "scopes": None,
                            "message": f"Authenticated as {username}. Fine-grained token detected — scope introspection not available. Verify the token has repository read/write permissions."}

            elif provider == "gitlab":
                resp = await client.get(
                    f"https://{hostname}/api/v4/user",
                    headers={"PRIVATE-TOKEN": credential},
                )
                if resp.status_code in (401, 403):
                    return {"ok": False, "username": None, "scopes": None,
                            "message": "Authentication failed — token may be invalid or expired"}
                if resp.status_code != 200:
                    return {"ok": False, "username": None, "scopes": None,
                            "message": f"GitLab returned {resp.status_code}"}

                username = resp.json().get("username")

                scopes = None
                ok = True
                scope_message = ""
                try:
                    tok_resp = await client.get(
                        f"https://{hostname}/api/v4/personal_access_tokens/self",
                        headers={"PRIVATE-TOKEN": credential},
                    )
                    if tok_resp.status_code == 200:
                        tok_data = tok_resp.json()
                        scopes = tok_data.get("scopes", [])
                        has_api = "api" in scopes
                        if has_api:
                            scope_message = f"Authenticated as {username}. Required scopes present."
                            ok = True
                        elif scopes:
                            # Scopes present but api not among them — insufficient for MR creation
                            scope_message = (
                                "Token is missing required scopes. "
                                "Classic PAT requires 'api' scope. "
                                "Fine-grained PAT requires Repository (read, write) + Merge Request (read, create)."
                            )
                            ok = False
                        else:
                            # Empty scopes — likely fine-grained token; cannot fully introspect
                            scope_message = (
                                f"Authenticated as {username}. Scope introspection returned no scopes — "
                                "token may be fine-grained. Ensure it has Repository (read, write) "
                                "and Merge Request (read, create) permissions."
                            )
                            ok = True
                    else:
                        scope_message = f"Authenticated as {username}. Could not introspect token scopes (endpoint returned non-200). Auth confirmed."
                        ok = True
                except Exception:
                    scope_message = f"Authenticated as {username}. Could not introspect token scopes. Auth confirmed."
                    ok = True

                return {"ok": ok, "username": username, "scopes": scopes, "message": scope_message}

            elif provider == "bitbucket":
                email_part, _, token_part = credential.partition(":")
                if not token_part:
                    return {"ok": False, "username": None, "scopes": None,
                            "message": "Credential must be in email:api_token format"}
                resp = await client.get(
                    "https://api.bitbucket.org/2.0/user",
                    auth=(email_part, token_part),
                )
                if resp.status_code in (401, 403):
                    return {"ok": False, "username": None, "scopes": None,
                            "message": "Authentication failed — API token may be invalid or expired"}
                if resp.status_code == 200:
                    data = resp.json()
                    resolved = data.get("username") or data.get("account_id")

                    scopes_header = resp.headers.get("x-oauth-scopes", "")
                    if scopes_header:
                        scopes = [s.strip() for s in scopes_header.split(",") if s.strip()]
                        required = {
                            "read:repository:bitbucket",
                            "write:repository:bitbucket",
                            "read:pullrequest:bitbucket",
                            "write:pullrequest:bitbucket",
                            "read:user:bitbucket",
                        }
                        missing = sorted(required - set(scopes))
                        if not missing:
                            return {"ok": True, "username": resolved, "scopes": scopes,
                                    "message": f"Authenticated as {resolved}. All required scopes present."}
                        missing_str = ", ".join(missing)
                        return {"ok": True, "username": resolved, "scopes": scopes,
                                "missing_scopes": missing,
                                "message": f"Authenticated as {resolved} but missing scopes: {missing_str}. Add these when creating your API token."}
                    return {"ok": True, "username": resolved, "scopes": None,
                            "message": f"Authenticated as {resolved}. Verify your API token has the required scopes."}
                return {"ok": False, "username": None, "scopes": None,
                        "message": f"Bitbucket returned {resp.status_code} — check credentials"}

    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException):
        return {"ok": False, "username": None, "scopes": None,
                "message": f"Could not reach {provider} — check hostname and connectivity"}
    except Exception as e:
        return {"ok": False, "username": None, "scopes": None,
                "message": f"Connection error: {e}"}

    return {"ok": False, "username": None, "scopes": None, "message": "Unknown provider"}


async def _handle_put_git_credential(receive, send, scope, provider):
    """PUT /dashboard/api/settings/git-credentials/{provider} — save/update credential."""
    user = scope.get("session_user") or {}
    if not _is_admin(user):
        return await _error(send, "Forbidden", 403)

    if provider not in _GIT_PROVIDERS:
        return await _error(send, f"Unknown provider '{provider}'. Must be one of: {', '.join(_GIT_PROVIDERS)}", 400)

    body = await _read_body(receive)
    data = json.loads(body) if body else {}

    credential = (data.get("credential") or "").strip()
    if not credential:
        return await _error(send, "credential is required", 400)

    hostname = (data.get("hostname") or "").strip() or _GIT_DEFAULT_HOSTNAMES[provider]
    credential_last4 = credential[-4:] if len(credential) >= 4 else credential
    encrypted = encrypt_value(credential)

    existing = await db.get_credential_by_provider(provider)
    if existing:
        await db.update_credential(existing["id"], credential=encrypted, hostname=hostname, credential_last4=credential_last4)
    else:
        await db.create_credential(provider, encrypted, hostname, credential_last4=credential_last4)

    # Run auth check after saving — non-blocking, save always succeeds
    auth = await _check_credential_auth(provider, credential, hostname)
    if not auth["ok"]:
        return await _json_response(send, {
            "ok": True,
            "warning": "Token saved but authentication failed — check that it's correct",
        })
    return await _json_response(send, {
        "ok": True,
        "username": auth["username"],
        "scopes": auth["scopes"],
    })


async def _handle_delete_git_credential(send, scope, provider):
    """DELETE /dashboard/api/settings/git-credentials/{provider} — remove credential."""
    user = scope.get("session_user") or {}
    if not _is_admin(user):
        return await _error(send, "Forbidden", 403)

    if provider not in _GIT_PROVIDERS:
        return await _error(send, f"Unknown provider '{provider}'", 400)

    existing = await db.get_credential_by_provider(provider)
    if not existing:
        return await _error(send, f"No {provider} credential configured", 404)

    await db.delete_credential(existing["id"])
    await _json_response(send, {"ok": True})


async def _handle_test_git_credential(send, scope, provider):
    """POST /dashboard/api/settings/git-credentials/{provider}/test — validate credential with scope check."""
    user = scope.get("session_user") or {}
    if not _is_admin(user):
        return await _error(send, "Forbidden", 403)

    if provider not in _GIT_PROVIDERS:
        return await _error(send, f"Unknown provider '{provider}'", 400)

    existing = await db.get_credential_by_provider(provider)
    if not existing or not existing.get("credential"):
        return await _json_response(send, {
            "ok": False, "username": None, "scopes": None,
            "message": f"No {provider} credential configured",
        })

    raw_cred = existing["credential"]
    credential = decrypt_value(raw_cred) if is_fernet_token(raw_cred) else raw_cred
    hostname = existing.get("hostname") or _GIT_DEFAULT_HOSTNAMES[provider]

    result = await _check_credential_auth(provider, credential, hostname)
    return await _json_response(send, result)


# ── User settings ─────────────────────────────────────────────────────────────

async def _handle_get_user_settings(scope, send):
    user = scope.get("session_user") or {}
    user_id = user.get("id")
    user_email = user.get("email")
    if not user_id:
        return await _error(send, "Not authenticated", 401)

    full_user = await db.get_user_by_email_with_auth(user_email)
    if not full_user:
        return await _error(send, "User not found", 404)

    creds = await db.get_user_credentials(user_id) or {}
    ant_key = creds.get("anthropic_api_key")
    credential_bypass = _settings.SKIP_CREDENTIAL_CHECK
    anthropic_info = {
        "configured": bool(ant_key),
        "key_last4": ant_key[-4:] if ant_key else None,
        "skip_credential_check": credential_bypass,
    }
    notif_prefs = creds.get("notification_preferences") or {}

    # Git credential status — any provider configured in git_credentials table
    git_credentials = await db.list_credentials()
    git_credential_configured = len(git_credentials) > 0

    await _json_response(send, {
        "profile": {
            "name": full_user.get("name"),
            "email": full_user.get("email"),
            "timezone": full_user.get("timezone"),
            "role": full_user.get("role"),
            "has_password": bool(full_user.get("password_hash")),
        },
        "anthropic": anthropic_info,
        "git_credential": {"configured": git_credential_configured},
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
        # Empty string means "remove the key" — store NULL so configured=False
        cred_updates["anthropic_api_key"] = data["anthropic_api_key"] or None
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


# ── API token management endpoints ────────────────────────────────────────

async def _handle_list_tokens(scope, send):
    user = scope.get("session_user") or {}
    user_id = user.get("id")
    if not user_id:
        return await _error(send, "Not authenticated", 401)

    tokens = await db.list_api_tokens(user_id)
    await _json_response(send, {"tokens": tokens})


async def _handle_create_token(receive, scope, send):
    user = scope.get("session_user") or {}
    user_id = user.get("id")
    if not user_id:
        return await _error(send, "Not authenticated", 401)

    body = await _read_body(receive)
    data = json.loads(body) if body else {}
    name = (data.get("name") or "").strip() or None

    result = await db.create_api_token(user_id, name=name)
    await _json_response(send, result, status=201)


async def _handle_revoke_token(scope, send, token_id_str):
    user = scope.get("session_user") or {}
    user_id = user.get("id")
    if not user_id:
        return await _error(send, "Not authenticated", 401)

    try:
        token_id = int(token_id_str)
    except (ValueError, TypeError):
        return await _error(send, "Invalid token ID", 400)

    # Verify the token belongs to this user before revoking
    tokens = await db.list_api_tokens(user_id)
    if not any(t["id"] == token_id for t in tokens):
        return await _error(send, "Token not found", 404)

    deleted = await db.revoke_api_token(token_id)
    if not deleted:
        return await _error(send, "Token not found", 404)
    await _json_response(send, {"ok": True})


# ── File upload constants ──────────────────────────────────────────────────

def _human_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f}MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes}B"


def _uploads_dir() -> Path:
    """Return the uploads directory (worker-accessible, outside /data)."""
    from switchboard.config.settings import UPLOADS_DIR
    return Path(UPLOADS_DIR)

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


def _parse_multipart(body: bytes, boundary: bytes) -> tuple[str | None, bytes | None, dict]:
    """Parse a multipart body and return (filename, file_data, form_fields).

    form_fields maps field names (str) to their string values for non-file parts.
    """
    from python_multipart.multipart import MultipartParser, parse_options_header

    filename_holder = [None]
    data_chunks = []
    in_file = [False]
    current_header_field = [b""]
    current_header_value = [b""]
    headers = [{}]
    form_fields = {}
    current_field_name = [None]
    field_chunks = []

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
        field_name = params.get(b"name")
        if fname is not None:
            filename_holder[0] = fname.decode("utf-8", errors="replace")
            in_file[0] = True
            current_field_name[0] = None
        else:
            in_file[0] = False
            current_field_name[0] = field_name.decode("utf-8", errors="replace") if field_name else None
            field_chunks.clear()

    def on_part_data(data, start, end):
        if in_file[0]:
            data_chunks.append(bytes(data[start:end]))
        elif current_field_name[0] is not None:
            field_chunks.append(bytes(data[start:end]))

    def on_part_end():
        if not in_file[0] and current_field_name[0] is not None:
            form_fields[current_field_name[0]] = b"".join(field_chunks).decode("utf-8", errors="replace")
            field_chunks.clear()
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
        return None, None, form_fields

    return filename_holder[0], b"".join(data_chunks), form_fields


# ── File handlers ──────────────────────────────────────────────────────────

async def _handle_list_files(scope, send):
    params = _parse_qs(scope)
    task_id = params.get("task_id") or None
    project_id = params.get("project_id") or None
    files = await db.list_files(task_id=task_id, project_id=project_id)
    await _json_response(send, files)


async def _handle_task_files(send, task_id: str):
    task = await db.get_task(task_id)
    if not task:
        return await _error(send, f"Task '{task_id}' not found", 404)
    files = await db.list_files(task_id=task_id)
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

    filename, file_data, form_fields = _parse_multipart(body, boundary)
    if filename is None or file_data is None:
        return await _error(send, "No file found in request", 400)

    # Sanitize filename — strip all directory components to prevent path traversal
    filename = Path(filename).name
    if not filename:
        return await _error(send, "Invalid filename", 400)

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

    # Optional task_id from form fields
    task_id = form_fields.get("task_id") or None
    if task_id:
        task = await db.get_task(task_id)
        if not task:
            return await _error(send, f"Task '{task_id}' not found", 404)

    # Optional project_id from form fields
    project_id = form_fields.get("project_id") or None
    if project_id:
        project = await db.get_project(project_id)
        if not project:
            return await _error(send, f"Project '{project_id}' not found", 404)

    # Save to disk
    file_id = str(uuid.uuid4())
    uploads_dir = _uploads_dir() / file_id
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
        task_id=task_id,
        project_id=project_id,
    )

    # Reactive injection: notify CC if task is currently working
    if task_id and task.get("status") == "working":
        human_size = _human_size(len(file_data))
        try:
            await db.post_task_message(
                task_id=task_id,
                author="switchboard",
                type="note",
                content=f"📎 File uploaded: {dest} ({mime_type}, {human_size})\nRead this file if relevant to your current work.",
            )
        except Exception:
            pass  # Non-blocking — upload still succeeds

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

    # Sanitize filename — strip all directory components to prevent path traversal
    new_name = Path(new_name).name
    if not new_name:
        return await _error(send, "Invalid filename", 400)

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

    new_mime = MIME_TYPES.get(ext)
    updated = await db.update_file(file_id, new_name, str(new_path), mime_type=new_mime)
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
    if uuid_dir.exists() and uuid_dir.parent == _uploads_dir():
        shutil.rmtree(uuid_dir, ignore_errors=True)

    await db.delete_file(file_id)
    await _json_response(send, {"ok": True})


async def _handle_download_file(send, file_id: str, scope):
    user = scope.get("session_user") or {}
    if not user.get("id"):
        return await _error(send, "Not authenticated", 401)

    record = await db.get_file(file_id)
    if not record:
        return await _error(send, f"File '{file_id}' not found", 404)

    stored = Path(record["stored_path"])
    if not stored.exists():
        return await _error(send, "File not found on disk", 404)

    data = stored.read_bytes()
    mime_type = record.get("mime_type") or "application/octet-stream"
    filename = record["filename"]
    encoded_name = quote(filename)
    disposition = f'attachment; filename="{filename}"; filename*=UTF-8\'\'{encoded_name}'

    await send({
        "type": "http.response.start", "status": 200,
        "headers": [
            [b"content-type", mime_type.encode()],
            [b"content-disposition", disposition.encode()],
            [b"content-length", str(len(data)).encode()],
            [b"cache-control", b"private, no-cache"],
        ],
    })
    await send({"type": "http.response.body", "body": data})


async def _handle_get_file(send, file_id: str, scope):
    """GET /dashboard/api/files/{id} — unified get file, works for any scope."""
    user = scope.get("session_user") or {}
    if not user.get("id"):
        return await _error(send, "Not authenticated", 401)

    record = await db.get_file(file_id)
    if not record:
        return await _error(send, f"File '{file_id}' not found", 404)

    from switchboard.server.handlers.files_handler import _is_readable, READABLE_EXTENSIONS
    filename = record.get("filename", "")
    readable = _is_readable(filename)
    record["readable"] = readable
    await _json_response(send, record)


async def _handle_promote_file(receive, send, file_id: str, scope):
    """POST /dashboard/api/files/{id}/promote — set project_id on an existing task file."""
    user = scope.get("session_user") or {}
    if not user.get("id"):
        return await _error(send, "Not authenticated", 401)

    body = await _read_body(receive)
    data = json.loads(body) if body else {}
    project_id = data.get("project_id", "").strip()
    if not project_id:
        return await _error(send, "project_id is required")

    project = await db.get_project(project_id)
    if not project:
        return await _error(send, f"Project '{project_id}' not found", 404)

    record = await db.promote_task_file(file_id, project_id)
    if not record:
        return await _error(send, f"File '{file_id}' not found or has no task_id — only task files can be promoted", 400)

    await _json_response(send, record)


async def _handle_search_api(scope, send):
    """GET /dashboard/api/search — unified semantic search for the dashboard."""
    from switchboard.server.handlers.search import _handle_search

    params = _parse_qs(scope)
    q = params.get("q", "").strip()
    if not q:
        return await _error(send, "Missing required query parameter: q", 400)

    project_id = params.get("project_id") or None
    try:
        limit = int(params.get("limit", 10))
    except ValueError:
        limit = 10

    result = await _handle_search({
        "query": q,
        "project_id": project_id,
        "limit": limit,
    })

    if "error" in result:
        return await _error(send, result["error"], 503)

    await _json_response(send, result)
