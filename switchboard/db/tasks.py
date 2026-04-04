"""Task CRUD, checklist, artifacts, tags, subtasks, and state definitions."""
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

from switchboard.config.constants import (
    TASK_MUTABLE_FIELDS,
    CORE_STATE_DEFINITIONS,
)
from switchboard.db.connection import get_db
from switchboard.db._helpers import now_iso, _strip_embedding, _read_messages, _determine_attempt_outcome


async def create_task(
    id: str, project_id: str, goal: str, branch: str | None = None,
    max_turns: int | None = None, max_wall_clock: int | None = None,
    jira_ticket: str | None = None, conversation_id: str | None = None,
    model: str | None = None, auto_test: bool = True,
    depends_on: str | None = None,
    auto_review: bool = True, review_model: str | None = None,
    parent_task_id: str | None = None, auto_pr: bool = False,
    component_id: str | None = None,
    claude_chat_url: str | None = None,
    auto_merge: bool = False,
    auto_release_worktree: bool = True,
    max_test_retries: int | None = None,
    max_review_retries: int | None = None,
    base_branch: str | None = None,
    created_by: int | None = None,
    dispatched_by: int | None = None,
) -> dict:
    async with get_db() as db:
        # Verify project exists
        rows = await db.execute_fetchall("SELECT id FROM projects WHERE id = ?", (project_id,))
        if not rows:
            raise ValueError(f"Project '{project_id}' not found")

        ts = now_iso()
        # Use short name (after project prefix) for branch to avoid slash issues
        short_name = id.split("/")[-1] if "/" in id else id
        branch = branch or short_name
        await db.execute(
            """INSERT INTO tasks
               (id, project_id, goal, status, branch, max_turns, max_wall_clock,
                jira_ticket, conversation_id, model, auto_test, depends_on,
                auto_review, review_model, parent_task_id, auto_pr, component_id,
                claude_chat_url, auto_merge, auto_release_worktree,
                max_test_retries, max_review_retries, base_branch,
                created_by, dispatched_by, created_at, updated_at)
               VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, project_id, goal, branch, max_turns, max_wall_clock,
             jira_ticket, conversation_id, model, auto_test, depends_on,
             auto_review, review_model, parent_task_id, auto_pr, component_id,
             claude_chat_url, auto_merge, auto_release_worktree,
             max_test_retries, max_review_retries, base_branch,
             created_by, dispatched_by, ts, ts),
        )
        await db.commit()

        # Write audit log for task creation
        from switchboard.db.audit import write_audit_log
        await write_audit_log(
            task_id=id, action="created",
            triggered_by="user",
            source_detail="create_task",
            previous_status=None, new_status="ready",
        )

        return {
            "id": id, "project_id": project_id, "goal": goal, "status": "ready",
            "phase": None, "branch": branch, "worktree_path": None,
            "max_turns": max_turns, "max_wall_clock": max_wall_clock,
            "jira_ticket": jira_ticket, "conversation_id": conversation_id,
            "model": model, "auto_test": auto_test, "depends_on": depends_on,
            "auto_review": auto_review, "review_model": review_model,
            "parent_task_id": parent_task_id, "auto_pr": auto_pr,
            "component_id": component_id, "claude_chat_url": claude_chat_url,
            "auto_merge": auto_merge, "auto_release_worktree": auto_release_worktree,
            "max_test_retries": max_test_retries, "max_review_retries": max_review_retries,
            "base_branch": base_branch,
            "created_by": created_by, "dispatched_by": dispatched_by,
            "created_at": ts, "updated_at": ts,
        }


async def get_task(id: str) -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (id,))
        if not rows:
            return None
        return _strip_embedding(dict(rows[0]))


async def update_task(id: str, **fields) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (id,))
        if not rows:
            raise ValueError(f"Task '{id}' not found")

        # Extract tags — handled separately via task_tags table
        tags = fields.pop("tags", None)

        # Validate component_id if provided
        if "component_id" in fields and fields["component_id"] is not None:
            comp_rows = await db.execute_fetchall(
                "SELECT id FROM components WHERE id = ?", (fields["component_id"],)
            )
            if not comp_rows:
                raise ValueError(f"Component '{fields['component_id']}' not found")

        # Filter to allowed fields to prevent SQL column injection
        col_fields = {k: v for k, v in fields.items() if k in TASK_MUTABLE_FIELDS}
        col_fields["updated_at"] = now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in col_fields)
        values = list(col_fields.values()) + [id]
        await db.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)

        # Update tags if provided
        if tags is not None:
            await db.execute("DELETE FROM task_tags WHERE task_id = ?", (id,))
            for tag in tags:
                await db.execute(
                    "INSERT OR IGNORE INTO task_tags (task_id, tag) VALUES (?, ?)",
                    (id, tag.strip().lower()),
                )

        await db.commit()

        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (id,))
        result = _strip_embedding(dict(rows[0]))
        tag_rows = await db.execute_fetchall(
            "SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (id,)
        )
        result["tags"] = [r["tag"] for r in tag_rows]
        return result


async def bulk_update_tasks(task_ids: list[str], **fields) -> int:
    """Apply the same field updates to multiple tasks. Returns count of updated tasks."""
    count = 0
    for task_id in task_ids:
        try:
            await update_task(task_id, **fields)
            count += 1
        except ValueError:
            pass  # skip tasks that don't exist
    return count


async def move_task(task_id: str, component_id: str) -> dict:
    """Reassign a task to a component. Validates component exists and belongs to same project."""
    async with get_db() as db:
        task_rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not task_rows:
            raise ValueError(f"Task '{task_id}' not found")
        task = dict(task_rows[0])

        comp_rows = await db.execute_fetchall("SELECT * FROM components WHERE id = ?", (component_id,))
        if not comp_rows:
            raise ValueError(f"Component '{component_id}' not found")
        component = dict(comp_rows[0])

        if component["project_id"] != task["project_id"]:
            raise ValueError(
                f"Component '{component_id}' belongs to project '{component['project_id']}', "
                f"but task '{task_id}' belongs to project '{task['project_id']}'"
            )

    return await update_task(task_id, component_id=component_id)


async def list_tasks(
    project_id: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    component_id: str | None = None,
    active_only: bool = False,
    query: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 50,
    sort: str = "date",
) -> list[dict]:
    from switchboard.db.search import sanitize_fts_query

    # Sanitize FTS query early — return empty list if query yields nothing
    fts_query: str | None = None
    if query is not None:
        fts_query = sanitize_fts_query(query)
        if fts_query is None:
            return []

    # When a text query is active and no explicit non-date sort requested, use relevance
    effective_sort = sort
    if fts_query is not None and sort == "date":
        effective_sort = "relevance"

    async with get_db() as db:
        conditions = []
        params: list = []

        if project_id:
            conditions.append("t.project_id = ?")
            params.append(project_id)
        if status:
            conditions.append("t.status = ?")
            params.append(status)
        if component_id:
            conditions.append("t.component_id = ?")
            params.append(component_id)
        if tag:
            conditions.append("EXISTS (SELECT 1 FROM task_tags tt WHERE tt.task_id = t.id AND tt.tag = ?)")
            params.append(tag.strip().lower())
        if active_only:
            # Exclude cancelled tasks and stale error/conflict tasks that have exhausted retries
            conditions.append("t.status != 'cancelled'")
            conditions.append(
                "NOT (t.pr_status IN ('error', 'conflict') AND t.gate_retries >= t.max_gate_retries)"
            )
        if after:
            conditions.append("t.created_at > ?")
            params.append(after)
        if before:
            conditions.append("t.created_at < ?")
            params.append(before)

        if fts_query is not None:
            conditions.append("tasks_fts MATCH ?")
            params.append(fts_query)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        fts_join = "JOIN tasks_fts ON tasks_fts.rowid = t.rowid" if fts_query is not None else ""
        bm25_select = ", bm25(tasks_fts) as bm25_score" if fts_query is not None else ""

        sort_clause = {
            "date": "t.updated_at DESC",
            "created": "t.created_at DESC",
            "status": "t.status ASC, t.updated_at DESC",
            "cost": "t.total_cost_usd DESC",
            "relevance": "bm25(tasks_fts) ASC",
        }.get(effective_sort, "t.updated_at DESC")

        # Fall back to date sort if relevance requested but no FTS join
        if effective_sort == "relevance" and fts_query is None:
            sort_clause = "t.updated_at DESC"

        sql = f"""
            SELECT t.*{bm25_select},
                (SELECT COUNT(*) FROM task_checklist WHERE task_id = t.id) as checklist_total,
                (SELECT COUNT(*) FROM task_checklist WHERE task_id = t.id AND done = TRUE) as checklist_done,
                (SELECT ref FROM task_artifacts WHERE task_id = t.id AND type = 'pr_url' LIMIT 1) as pr_url
            FROM tasks t
            {fts_join}
            {where}
            ORDER BY {sort_clause}
            LIMIT ?
        """
        params.append(limit)

        rows = await db.execute_fetchall(sql, params)
        tasks = []
        for r in rows:
            task = _strip_embedding(dict(r))
            # Fetch tags for each task
            tag_rows = await db.execute_fetchall(
                "SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (task["id"],),
            )
            task["tags"] = [tr["tag"] for tr in tag_rows]
            tasks.append(task)
        return tasks


async def get_tasks_with_open_prs() -> list[dict]:
    """Return tasks that have a pr_url but whose pr_status is not 'merged' or 'closed'."""
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT t.*,
                (SELECT ref FROM task_artifacts WHERE task_id = t.id AND type = 'pr_url' LIMIT 1) as pr_url
            FROM tasks t
            WHERE EXISTS (
                SELECT 1 FROM task_artifacts WHERE task_id = t.id AND type = 'pr_url'
            )
            AND (t.pr_status IS NULL OR t.pr_status NOT IN ('merged', 'closed'))
        """)
        return [dict(r) for r in rows]


async def get_project_task_counts() -> dict[str, dict]:
    """Get task counts and total cost per project in a single query."""
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT project_id,
                   COUNT(*) as total_tasks,
                   SUM(CASE WHEN status = 'working' THEN 1 ELSE 0 END) as active_task_count,
                   COALESCE(SUM(total_cost_usd), 0) as total_cost
            FROM tasks
            GROUP BY project_id
        """)
        return {
            r["project_id"]: {
                "total_tasks": r["total_tasks"],
                "active_task_count": r["active_task_count"],
                "total_cost": round(r["total_cost"], 2),
            }
            for r in rows
        }


async def get_recent_activity(limit: int = 5) -> list[dict]:
    """Get recent significant task messages across all projects."""
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT m.task_id, m.type AS event_type, m.title, m.created_at
            FROM messages m
            WHERE m.type IN ('result', 'status', 'review', 'question')
            ORDER BY m.created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]


async def get_dependents(task_id: str) -> list[dict]:
    """Get tasks that depend on the given task."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM tasks WHERE depends_on = ? ORDER BY created_at", (task_id,),
        )
        return [_strip_embedding(dict(r)) for r in rows]


async def get_chain(task_id: str) -> list[dict]:
    """Walk the dependency chain from root to tail, given any member."""
    async with get_db() as db:
        # Walk up to find root
        current_id = task_id
        visited = set()
        while True:
            if current_id in visited:
                break
            visited.add(current_id)
            rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (current_id,))
            if not rows:
                break
            task = _strip_embedding(dict(rows[0]))
            if not task.get("depends_on"):
                break
            current_id = task["depends_on"]

        # Walk down from root
        chain = []
        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (current_id,))
        if rows:
            chain.append(_strip_embedding(dict(rows[0])))
            while True:
                deps = await db.execute_fetchall(
                    "SELECT * FROM tasks WHERE depends_on = ? ORDER BY created_at LIMIT 1",
                    (chain[-1]["id"],),
                )
                if not deps:
                    break
                chain.append(_strip_embedding(dict(deps[0])))
        return chain


async def count_active_tasks() -> int:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status = 'working'"
        )
        return rows[0]["cnt"]


async def get_working_tasks_for_conversation(conversation_id: str) -> list[str]:
    """Return task IDs that are currently working and linked to the given conversation."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT id FROM tasks WHERE status = 'working' AND conversation_id = ?",
            (conversation_id,),
        )
        return [r["id"] for r in rows]


async def get_queued_tasks() -> list[dict]:
    """Return ready tasks with queued_at set, ordered FIFO (oldest first).

    Excludes tasks whose depends_on parent hasn't gate-passed yet.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT t.* FROM tasks t
               WHERE t.status = 'ready'
                 AND t.queued_at IS NOT NULL
                 AND (
                   t.depends_on IS NULL
                   OR EXISTS (
                     SELECT 1 FROM tasks p
                     WHERE p.id = t.depends_on AND p.gate_passed_at IS NOT NULL
                   )
                 )
               ORDER BY t.recovery_priority DESC, t.queued_at ASC"""
        )
        return [dict(r) for r in rows]


async def post_task_message(
    task_id: str, author: str, content: str,
    type: str | None = None, title: str | None = None, pinned: bool = False,
    user_id: int | None = None,
) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id, current_attempt FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

        attempt_number = rows[0]["current_attempt"] or 1

        if pinned:
            await db.execute(
                "UPDATE messages SET pinned = FALSE WHERE task_id = ? AND pinned = TRUE",
                (task_id,),
            )

        ts = now_iso()
        cursor = await db.execute(
            """INSERT INTO messages (task_id, author, type, title, content, pinned, user_id, created_at, attempt_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, author, type, title, content, pinned, user_id, ts, attempt_number),
        )
        msg_id = cursor.lastrowid

        await db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (ts, task_id))
        await db.commit()
        return {
            "id": msg_id, "task_id": task_id, "author": author,
            "type": type, "title": title, "content": content,
            "pinned": pinned, "user_id": user_id, "created_at": ts, "attempt_number": attempt_number,
        }


async def read_task_messages(
    task_id: str, last_n: int | None = None, after: int | None = None,
    type: str | None = None, offset: int | None = None,
    limit: int | None = None, attempt: int | None = None,
) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

    return await _read_messages(
        filter_column="task_id", filter_value=task_id,
        last_n=last_n, after=after, type=type,
        offset=offset, limit=limit, attempt=attempt,
    )


async def get_task_pinned(task_id: str) -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE task_id = ? AND pinned = TRUE LIMIT 1",
            (task_id,),
        )
        return _strip_embedding(dict(rows[0])) if rows else None


async def get_message_by_id(message_id: int) -> dict | None:
    """Fetch a single message by its ID. Returns None if not found."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE id = ?",
            (message_id,),
        )
        return _strip_embedding(dict(rows[0])) if rows else None


async def set_message_embedding(message_id: int, embedding_blob: bytes) -> None:
    """Store a packed float32 embedding blob on a message row and update messages_vec."""
    async with get_db() as db:
        await db.execute(
            "UPDATE messages SET embedding = ? WHERE id = ?",
            (embedding_blob, message_id),
        )
        # Keep messages_vec in sync — only for standard 1536-dim embeddings (6144 bytes)
        if len(embedding_blob) == 1536 * 4:
            try:
                await db.execute(
                    "INSERT OR REPLACE INTO messages_vec(rowid, embedding) VALUES (?, ?)",
                    (message_id, embedding_blob),
                )
            except Exception as e:
                log.warning("vec0 insert failed for message %d: %s", message_id, e)
        await db.commit()


async def get_task_status(task_id: str) -> dict:
    """Get comprehensive task status including checklist, messages, artifacts, tags."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

        task = _strip_embedding(dict(rows[0]))

        # Checklist summary
        cl_rows = await db.execute_fetchall(
            "SELECT * FROM task_checklist WHERE task_id = ? ORDER BY id", (task_id,),
        )
        checklist = [dict(r) for r in cl_rows]
        task["checklist"] = checklist
        task["checklist_total"] = len(checklist)
        task["checklist_done"] = sum(1 for c in checklist if c["done"])

        # Recent messages (last 5)
        msg_rows = await db.execute_fetchall(
            """SELECT * FROM messages WHERE task_id = ?
               ORDER BY created_at DESC LIMIT 5""",
            (task_id,),
        )
        task["recent_messages"] = [_strip_embedding(dict(r)) for r in reversed(msg_rows)]

        # Artifacts
        art_rows = await db.execute_fetchall(
            "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        )
        task["artifacts"] = [dict(r) for r in art_rows]

        # Tags
        tag_rows = await db.execute_fetchall(
            "SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (task_id,),
        )
        task["tags"] = [r["tag"] for r in tag_rows]

        # Review subtask (from subtasks table, type='review', most recent)
        review_rows = await db.execute_fetchall(
            """SELECT id, status, model, created_at, completed_at
               FROM subtasks WHERE task_id = ? AND type = 'review'
               ORDER BY rowid DESC LIMIT 1""",
            (task_id,),
        )
        if review_rows:
            rs = dict(review_rows[0])
            now_dt = datetime.now(timezone.utc)
            created_dt = datetime.fromisoformat(rs["created_at"].replace("Z", "+00:00"))
            if rs["status"] == "working" or not rs["completed_at"]:
                elapsed_s = int((now_dt - created_dt).total_seconds())
            else:
                completed_dt = datetime.fromisoformat(rs["completed_at"].replace("Z", "+00:00"))
                elapsed_s = int((completed_dt - created_dt).total_seconds())
            task["review_subtask"] = {
                "task_id": rs["id"],
                "status": rs["status"],
                "session_id": None,
                "elapsed": f"{elapsed_s}s",
                "model": rs["model"],
            }
        else:
            task["review_subtask"] = None

        # Parse last_test_output JSON if present
        if task.get("last_test_output"):
            try:
                task["last_test_output"] = json.loads(task["last_test_output"])
            except (json.JSONDecodeError, TypeError):
                pass  # leave as raw string

        return task


async def get_task_attempts(task_id: str) -> list[dict]:
    """Return messages grouped by attempt_number, each group with an outcome summary.

    Outcome values: "in-progress", "success", "test-failure", "review-rejection",
                    "wall-clock-timeout", "turns-exhausted", "error", "retried"
    """
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

        msg_rows = await db.execute_fetchall(
            """SELECT id, author, type, title, content, created_at, attempt_number
               FROM messages WHERE task_id = ? ORDER BY created_at ASC""",
            (task_id,),
        )
        messages = [dict(r) for r in msg_rows]

    # Group by attempt_number (default to 1 if NULL)
    groups: dict[int, list[dict]] = {}
    for msg in messages:
        attempt = msg.get("attempt_number") or 1
        groups.setdefault(attempt, []).append(msg)

    # Determine outcome for each attempt
    max_attempt = max(groups.keys()) if groups else 1
    result = []
    for attempt_num in sorted(groups.keys()):
        group_messages = groups[attempt_num]
        is_last = attempt_num == max_attempt
        outcome = _determine_attempt_outcome(group_messages, is_last, attempt_num < max_attempt)
        result.append({
            "attempt_number": attempt_num,
            "messages": group_messages,
            "outcome": outcome,
        })

    return result


def get_merged_state_definitions(project: dict | None = None) -> dict:
    """Merge core state definitions with project-level custom states."""
    merged = dict(CORE_STATE_DEFINITIONS)
    if project and project.get("state_definitions"):
        custom = project["state_definitions"]
        if isinstance(custom, str):
            custom = json.loads(custom)
        merged.update(custom)
    return merged


def get_state_definition(state: str, project: dict | None = None) -> dict:
    """Get the definition for a single state, with fallback."""
    merged = get_merged_state_definitions(project)
    if state in merged:
        return merged[state]
    # Unknown state — return a sensible default
    return {"color": "#6b7280", "label": state.replace("-", " ").title(), "pulse": False}


# ---------------------------------------------------------------------------
# Task Checklist
# ---------------------------------------------------------------------------

async def create_checklist_items(task_id: str, items: list[str]) -> list[dict]:
    async with get_db() as db:
        ts = now_iso()
        result = []
        for item in items:
            cursor = await db.execute(
                "INSERT INTO task_checklist (task_id, item, done, updated_at) VALUES (?, ?, FALSE, ?)",
                (task_id, item, ts),
            )
            result.append({"id": cursor.lastrowid, "task_id": task_id, "item": item, "done": False})
        await db.commit()
        return result


async def get_checklist(task_id: str) -> list[dict]:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM task_checklist WHERE task_id = ? ORDER BY id", (task_id,),
        )
        return [dict(r) for r in rows]


async def update_checklist_item(item_id: int, done: bool) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM task_checklist WHERE id = ?", (item_id,))
        if not rows:
            raise ValueError(f"Checklist item {item_id} not found")

        ts = now_iso()
        await db.execute(
            "UPDATE task_checklist SET done = ?, updated_at = ? WHERE id = ?",
            (done, ts, item_id),
        )
        await db.commit()
        item = dict(rows[0])
        item["done"] = done
        item["updated_at"] = ts
        return item


async def add_checklist_item(task_id: str, item: str) -> dict:
    """Add a new checklist item to a task."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

        ts = now_iso()
        cursor = await db.execute(
            "INSERT INTO task_checklist (task_id, item, done, updated_at) VALUES (?, ?, FALSE, ?)",
            (task_id, item, ts),
        )
        await db.commit()
        return {"id": cursor.lastrowid, "task_id": task_id, "item": item, "done": False, "updated_at": ts}


async def remove_checklist_item(item_id: int) -> dict:
    """Remove a checklist item by ID."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM task_checklist WHERE id = ?", (item_id,))
        if not rows:
            raise ValueError(f"Checklist item {item_id} not found")

        await db.execute("DELETE FROM task_checklist WHERE id = ?", (item_id,))
        await db.commit()
        item = dict(rows[0])
        item["removed"] = True
        return item


async def update_checklist_item_text(item_id: int, text: str) -> dict:
    """Update the text of a checklist item."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM task_checklist WHERE id = ?", (item_id,))
        if not rows:
            raise ValueError(f"Checklist item {item_id} not found")

        ts = now_iso()
        await db.execute(
            "UPDATE task_checklist SET item = ?, updated_at = ? WHERE id = ?",
            (text, ts, item_id),
        )
        await db.commit()
        item = dict(rows[0])
        item["item"] = text
        item["updated_at"] = ts
        return item


# ---------------------------------------------------------------------------
# Task Artifacts
# ---------------------------------------------------------------------------

async def add_artifact(task_id: str, type: str, ref: str) -> dict:
    async with get_db() as db:
        ts = now_iso()
        cursor = await db.execute(
            "INSERT INTO task_artifacts (task_id, type, ref, created_at) VALUES (?, ?, ?, ?)",
            (task_id, type, ref, ts),
        )
        await db.commit()
        return {"id": cursor.lastrowid, "task_id": task_id, "type": type, "ref": ref, "created_at": ts}


async def get_artifacts(task_id: str) -> list[dict]:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY created_at", (task_id,),
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Task Tags
# ---------------------------------------------------------------------------

async def set_task_tags(task_id: str, tags: list[str]) -> list[str]:
    """Set tags for a task (replaces existing tags)."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

        await db.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
        for tag in tags:
            await db.execute(
                "INSERT OR IGNORE INTO task_tags (task_id, tag) VALUES (?, ?)",
                (task_id, tag.strip().lower()),
            )
        await db.commit()
        return [t.strip().lower() for t in tags]


async def get_task_tags(task_id: str) -> list[str]:
    """Get tags for a task."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (task_id,),
        )
        return [r["tag"] for r in rows]


# ---------------------------------------------------------------------------
# Subtasks
# ---------------------------------------------------------------------------

async def create_subtask(id: str, task_id: str, type: str, prompt: str, model: str = "opus") -> dict:
    """Create a subtask record."""
    async with get_db() as conn:
        ts = now_iso()
        await conn.execute(
            """INSERT INTO subtasks (id, task_id, type, status, model, prompt, created_at)
               VALUES (?, ?, ?, 'working', ?, ?, ?)""",
            (id, task_id, type, model, prompt, ts),
        )
        await conn.commit()
        return {"id": id, "task_id": task_id, "type": type, "status": "working",
                "model": model, "prompt": prompt, "created_at": ts}


async def update_subtask(id: str, **fields) -> dict:
    """Update a subtask. Only allowed fields are updated."""
    async with get_db() as conn:
        allowed = {"status", "result", "input_tokens", "output_tokens",
                    "cost_usd", "duration_ms", "completed_at"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            rows = await conn.execute_fetchall("SELECT * FROM subtasks WHERE id = ?", (id,))
            return dict(rows[0]) if rows else {}
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [id]
        await conn.execute(f"UPDATE subtasks SET {set_clause} WHERE id = ?", values)
        await conn.commit()
        rows = await conn.execute_fetchall("SELECT * FROM subtasks WHERE id = ?", (id,))
        return dict(rows[0]) if rows else {}


async def get_subtasks(task_id: str) -> list[dict]:
    """Get all subtasks for a task, ordered by creation time."""
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM subtasks WHERE task_id = ? ORDER BY created_at", (task_id,),
        )
        return [dict(r) for r in rows]


async def get_subtask(id: str) -> dict | None:
    """Get a single subtask by ID."""
    async with get_db() as conn:
        rows = await conn.execute_fetchall("SELECT * FROM subtasks WHERE id = ?", (id,))
        return dict(rows[0]) if rows else None


# ---------------------------------------------------------------------------
# Task attempt records (per-attempt session_id tracking)
# ---------------------------------------------------------------------------


async def create_attempt(task_id: str, attempt_number: int) -> dict:
    """Create a new attempt record. Returns the created row."""
    ts = now_iso()
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO task_attempts (task_id, attempt_number, started_at)
               VALUES (?, ?, ?)""",
            (task_id, attempt_number, ts),
        )
        await conn.commit()
        rows = await conn.execute_fetchall(
            "SELECT * FROM task_attempts WHERE task_id = ? AND attempt_number = ?",
            (task_id, attempt_number),
        )
        return dict(rows[0])


async def update_attempt(task_id: str, attempt_number: int, **kwargs) -> None:
    """Update fields on an attempt record (session_id, finished_at, outcome)."""
    if not kwargs:
        return
    allowed = {"session_id", "finished_at", "outcome"}
    filtered = {k: v for k, v in kwargs.items() if k in allowed}
    if not filtered:
        return
    set_clause = ", ".join(f"{k} = ?" for k in filtered)
    values = list(filtered.values()) + [task_id, attempt_number]
    async with get_db() as conn:
        await conn.execute(
            f"UPDATE task_attempts SET {set_clause} WHERE task_id = ? AND attempt_number = ?",
            values,
        )
        await conn.commit()


async def get_attempt(task_id: str, attempt_number: int) -> dict | None:
    """Get a specific attempt record."""
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM task_attempts WHERE task_id = ? AND attempt_number = ?",
            (task_id, attempt_number),
        )
        return dict(rows[0]) if rows else None


async def get_previous_attempt_session_id(task_id: str, current_attempt: int) -> str | None:
    """Get the session_id from the attempt before current_attempt.

    Returns None if no previous attempt exists or it has no session_id.
    """
    if current_attempt <= 1:
        return None
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            """SELECT session_id FROM task_attempts
               WHERE task_id = ? AND attempt_number = ? AND session_id IS NOT NULL""",
            (task_id, current_attempt - 1),
        )
        return rows[0]["session_id"] if rows else None
