"""Task tool handlers."""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import switchboard.db as db
from switchboard.notifications import slack as notify
import switchboard.dispatch as task_engine
from switchboard.server.handlers.common import _embed_message_async, PR_URL_RE

log = logging.getLogger("switchboard.server")

_UPDATE_TASK_FIELDS = {
    "component_id", "base_branch", "branch_target", "tags",
    "auto_test", "auto_review", "auto_merge", "auto_pr",
    "max_test_retries", "max_review_retries",
    "model", "jira_ticket", "conversation_id", "claude_chat_url",
}


async def _handle_dispatch_task(arguments):
    # Auto-prefix task ID with project to avoid global collisions
    project_id = arguments["project_id"]
    raw_id = arguments["id"]
    task_id = f"{project_id}/{raw_id}" if "/" not in raw_id else raw_id
    result = await task_engine.dispatch_task(
        project_id=project_id,
        task_id=task_id,
        goal=arguments["goal"],
        spec=arguments.get("spec"),
        checklist=arguments.get("checklist"),
        phase=arguments.get("phase", "analysis"),
        max_turns=arguments.get("max_turns"),
        max_wall_clock=arguments.get("max_wall_clock"),
        escalation_criteria=arguments.get("escalation_criteria"),
        branch=arguments.get("branch"),
        jira_ticket=arguments.get("jira_ticket"),
        conversation_id=arguments.get("conversation_id"),
        model=arguments.get("model"),
        auto_test=arguments.get("auto_test", True),
        auto_review=arguments.get("auto_review", True),
        review_model=arguments.get("review_model"),
        auto_pr=arguments.get("auto_pr", False),
        auto_merge=arguments.get("auto_merge", False),
        auto_release_worktree=arguments.get("auto_release_worktree", True),
        base_branch=arguments.get("base_branch"),
        component_id=arguments.get("component_id"),
        claude_chat_url=arguments.get("claude_chat_url"),
        depends_on=(f"{project_id}/{arguments['depends_on']}"
                    if arguments.get("depends_on") and "/" not in arguments["depends_on"]
                    else arguments.get("depends_on")),
        held=arguments.get("held", False),
    )
    # Set tags if provided
    tags = arguments.get("tags")
    if tags:
        await db.set_task_tags(task_id, tags)
        result["tags"] = tags
    return result


async def _handle_release_worktree(arguments):
    return await task_engine.release_worktree(arguments["task_id"])


async def _handle_resume_task(arguments):
    return await task_engine.resume_task(arguments["task_id"])


async def _handle_approve_task(arguments):
    return await task_engine.approve_task(arguments["task_id"])


async def _handle_retry_task(arguments):
    return await task_engine.retry_task(
        task_id=arguments["task_id"],
        clean=arguments.get("clean", False),
    )


async def _handle_reopen_task(arguments):
    return await task_engine.reopen_task(arguments["task_id"])


async def _handle_start_reopened_task(arguments):
    kwargs = {"task_id": arguments["task_id"]}
    if "auto_test" in arguments:
        kwargs["auto_test"] = arguments["auto_test"]
    if "auto_review" in arguments:
        kwargs["auto_review"] = arguments["auto_review"]
    return await task_engine.start_reopened_task(**kwargs)


async def _handle_cancel_task(arguments):
    return await task_engine.cancel_task(arguments["task_id"])


async def _handle_close_task(arguments):
    return await task_engine.close_task(
        task_id=arguments["task_id"],
        cleanup=arguments.get("cleanup", True),
        force_delete_branch=arguments.get("force_delete_branch", False),
    )


async def _handle_get_task_status(arguments):
    result = await db.get_task_status(arguments["task_id"])

    # Liveness detection based on status + last_activity
    result["alive"] = result.get("status") == "working"
    stale_seconds = 0
    if result["alive"] and result.get("last_activity"):
        last = datetime.fromisoformat(result["last_activity"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - last).total_seconds()
        stale_seconds = round(age)
        result["stale"] = age > 900  # 15 minutes with no activity
        result["idle_minutes"] = round(age / 60, 1)
    else:
        result["stale"] = False
    result["stale_seconds"] = stale_seconds

    include_detail = arguments.get("include_detail", False)

    if include_detail:
        # Full response — add resolved config, PID check, state definition, optional log tail
        try:
            result["resolved_config"] = await db.resolve_config(arguments["task_id"])
        except Exception:
            pass
        if result.get("pid"):
            result["pid_alive"] = task_engine._is_pid_alive(result["pid"])
        project = await db.get_project(result.get("project_id", ""))
        result["state_definition"] = db.get_state_definition(result.get("status", ""), project)
        if arguments.get("include_log_tail") and result.get("worktree_path"):
            log_path = os.path.join(result["worktree_path"], ".switchboard", "cc-stderr.log")
            result["log_tail"] = task_engine._tail_file(log_path, 30)
        return result

    # Slim summary — only the fields a caller needs for "is this done yet?"
    recent = result.get("recent_messages") or []
    last_msg = recent[-1] if recent else None
    excerpt = None
    last_message_at = None
    if last_msg:
        content = last_msg.get("content") or ""
        excerpt = content[:120].replace("\n", " ").strip()
        last_message_at = last_msg.get("created_at")

    return {
        "task_id": result["id"],
        "status": result.get("status"),
        "phase": result.get("phase"),
        "gate_status": result.get("gate_status"),
        "alive": result["alive"],
        "stale": result["stale"],
        "idle_minutes": result.get("idle_minutes"),
        "checklist_done": result.get("checklist_done", 0),
        "checklist_total": result.get("checklist_total", 0),
        "total_cost_usd": result.get("total_cost_usd"),
        "pr_status": result.get("pr_status"),
        "last_message_excerpt": excerpt,
        "last_message_at": last_message_at,
    }


async def _handle_list_tasks(arguments):
    task_list = await db.list_tasks(
        project_id=arguments.get("project_id"),
        status=arguments.get("status"),
        tag=arguments.get("tag"),
        component_id=arguments.get("component_id"),
        active_only=arguments.get("active_only", True),  # MCP default: active tasks only
    )
    # Cache project lookups for state definitions
    project_cache: dict[str, dict | None] = {}
    for task in task_list:
        pid = task.get("project_id", "")
        if pid not in project_cache:
            project_cache[pid] = await db.get_project(pid)
        task["state_definition"] = db.get_state_definition(task.get("status", ""), project_cache[pid])
    return task_list


async def _handle_update_task(arguments):
    task_id = arguments["task_id"]
    fields = {k: v for k, v in arguments.items() if k in _UPDATE_TASK_FIELDS}
    return await db.update_task(task_id, **fields)


async def _handle_bulk_update_tasks(arguments):
    task_ids = arguments["task_ids"]
    fields = {k: v for k, v in arguments.items() if k in _UPDATE_TASK_FIELDS}
    count = await db.bulk_update_tasks(task_ids, **fields)
    return {"updated": count, "requested": len(task_ids)}


async def _handle_move_task(arguments):
    return await db.move_task(arguments["task_id"], arguments["component_id"])


async def _handle_update_task_checklist(arguments):
    result = await db.update_checklist_item(
        item_id=arguments["item_id"],
        done=arguments["done"],
    )
    # Notify on checklist progress
    if arguments.get("done") and result.get("task_id"):
        checklist = await db.get_checklist(result["task_id"])
        done_count = sum(1 for c in checklist if c.get("done"))
        await notify.checklist_progress(
            task_id=result["task_id"],
            item_text=result.get("item", ""),
            done=done_count,
            total=len(checklist),
        )
    return result


async def _handle_update_task_phase(arguments):
    fields = {}
    if "detail" in arguments:
        fields["phase"] = f"{arguments.get('phase', 'working')}: {arguments['detail']}"
    elif "phase" in arguments:
        fields["phase"] = arguments["phase"]
    fields["last_activity"] = db.now_iso()
    result = await db.update_task(arguments["task_id"], **fields)
    await notify.task_phase_changed(
        task_id=arguments["task_id"],
        phase=fields.get("phase", "working"),
    )
    return result


async def _handle_post_task_message(arguments):
    result = await db.post_task_message(
        task_id=arguments["task_id"],
        author=arguments["author"],
        content=arguments["content"],
        type=arguments.get("type"),
        title=arguments.get("title"),
        pinned=arguments.get("pinned", False),
    )
    # Async embed — fire and forget, doesn't block the response
    asyncio.create_task(
        _embed_message_async(result["id"], arguments["content"], arguments.get("type"))
    )
    # Notify Slack on progress, result, and question messages
    msg_type = arguments.get("type", "")
    if msg_type == "question":
        await notify.task_question(
            task_id=arguments["task_id"],
            question=arguments["content"],
        )
    elif msg_type in ("progress", "result"):
        await notify.task_progress(
            task_id=arguments["task_id"],
            title=arguments.get("title"),
            content=arguments["content"],
            msg_type=msg_type,
        )
    # Auto-extract PR URLs from result/progress messages
    if msg_type in ("result", "progress"):
        urls = PR_URL_RE.findall(arguments.get("content", ""))
        for url in urls:
            await db.add_artifact(arguments["task_id"], type="pr_url", ref=url)
    return result


async def _handle_read_task_messages(arguments):
    return await db.read_task_messages(
        task_id=arguments["task_id"],
        after=arguments.get("after"),
        last_n=arguments.get("last_n"),
        type=arguments.get("type"),
    )


def _resolve_log_dir(task: dict, project: dict | None, attempt: int | None) -> tuple[str | None, str | None]:
    """Return (log_dir_path, source_label) for reading logs.

    Priority:
    1. If attempt specified → read from archive
    2. If worktree exists → read from live worktree
    3. Fallback → read from highest-numbered archive
    Returns (path_or_None, label).
    """
    if attempt is not None:
        if not project:
            return None, "archive"
        archive = task_engine._find_archive_path(project, task["id"], attempt)
        return str(archive) if archive else None, f"archive attempt-{attempt}"

    worktree = task.get("worktree_path")
    if worktree and os.path.isdir(os.path.join(worktree, ".switchboard")):
        return os.path.join(worktree, ".switchboard"), "live"

    # Fallback to highest archive
    if project:
        archive = task_engine._find_archive_path(project, task["id"], None)
        if archive:
            return str(archive), "archive (latest)"

    return None, None


async def _handle_get_session_log(arguments):
    task_id = arguments["task_id"]
    task = await db.get_task(task_id)
    if not task:
        return {"error": f"Task '{task_id}' not found"}

    attempt = arguments.get("attempt")
    project = await db.get_project(task["project_id"]) if task.get("project_id") else None
    log_dir, source = _resolve_log_dir(task, project, attempt)

    if not log_dir:
        return {"error": "No log data found (no live worktree and no archived attempts)"}

    log_path = os.path.join(log_dir, "session.jsonl")
    if not os.path.isfile(log_path):
        return {"entries": [], "message": "No session log file found", "source": source}

    tail = arguments.get("tail", 50)
    type_filter = None
    if arguments.get("types"):
        type_filter = {t.strip() for t in arguments["types"].split(",")}

    entries = []
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if type_filter and entry.get("type") not in type_filter:
                    continue
                entries.append(entry)
    except Exception as e:
        return {"error": f"Failed to read session log: {e}"}

    # Apply tail
    entries = entries[-tail:]

    # Truncate large content fields to keep response size reasonable
    for entry in entries:
        if isinstance(entry.get("content"), list):
            for block in entry["content"]:
                for key in ("text", "preview", "input"):
                    if isinstance(block.get(key), str) and len(block[key]) > 500:
                        block[key] = block[key][:500] + "... [truncated]"
        if isinstance(entry.get("result"), str) and len(entry["result"]) > 500:
            entry["result"] = entry["result"][:500] + "... [truncated]"

    return {"entries": entries, "count": len(entries), "source": source}


async def _handle_get_dispatch_log(arguments):
    task_id = arguments["task_id"]
    task = await db.get_task(task_id)
    if not task:
        return {"error": f"Task '{task_id}' not found"}

    attempt = arguments.get("attempt")
    project = await db.get_project(task["project_id"]) if task.get("project_id") else None
    log_dir, source = _resolve_log_dir(task, project, attempt)

    if not log_dir:
        return {"error": "No log data found (no live worktree and no archived attempts)"}

    log_path = os.path.join(log_dir, "dispatch.log")
    if not os.path.isfile(log_path):
        return {"text": "", "message": "No dispatch log file found", "source": source}

    tail = arguments.get("tail", 20)
    try:
        with open(log_path) as f:
            lines = f.readlines()
        text = "".join(lines[-tail:])
    except Exception as e:
        return {"error": f"Failed to read dispatch log: {e}"}

    return {"text": text, "source": source}


async def _handle_list_attempts(arguments):
    return await task_engine.list_attempts(arguments["task_id"])


async def _handle_add_checklist_item(arguments):
    return await db.add_checklist_item(
        task_id=arguments["task_id"],
        item=arguments["item"],
    )


async def _handle_remove_checklist_item(arguments):
    return await db.remove_checklist_item(item_id=arguments["item_id"])


async def _handle_update_checklist_item_text(arguments):
    return await db.update_checklist_item_text(
        item_id=arguments["item_id"],
        text=arguments["text"],
    )


async def _handle_get_pipeline(arguments):
    chain = await db.get_chain(arguments["task_id"])
    current_idx = next((i for i, t in enumerate(chain) if t["id"] == arguments["task_id"]), -1)
    return {"chain": chain, "current_index": current_idx}


async def _handle_search_task_messages(arguments):
    return await db.search_task_messages(
        query=arguments["query"],
        project_id=arguments.get("project_id"),
        limit=arguments.get("limit", 20),
    )
