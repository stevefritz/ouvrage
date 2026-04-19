"""Component CRUD, conversation linking, and config resolution."""
import json

from ouvrage.config.constants import (
    COMPONENT_CONFIG_FIELDS,
    COMPONENT_MUTABLE_FIELDS,
    SYSTEM_DEFAULTS,
)
from ouvrage.db.connection import get_db
from ouvrage.db._helpers import now_iso


async def create_component(
    id: str, project_id: str, name: str,
    description: str | None = None, phase: str = "planning",
    review_ignore_patterns: list | None = None,
    created_by: int | None = None,
    # base_branch is not exposed in the MCP tool schema but is kept here because
    # git/operations.py:resolve_branch_target() reads component.base_branch to
    # determine the merge target. Internal callers (e.g. tests, migrations) may
    # still pass it directly.
    base_branch: str | None = None,
    **ignored_fields,
) -> dict:
    async with get_db() as db:
        # Verify project exists
        rows = await db.execute_fetchall("SELECT id FROM projects WHERE id = ?", (project_id,))
        if not rows:
            raise ValueError(f"Project '{project_id}' not found")

        ts = now_iso()
        rip = json.dumps(review_ignore_patterns) if isinstance(review_ignore_patterns, list) else None
        await db.execute(
            "INSERT INTO components (id, project_id, name, description, phase, review_ignore_patterns, base_branch, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [id, project_id, name, description, phase, rip, base_branch, created_by, ts, ts],
        )
        await db.commit()

        return {
            "id": id, "project_id": project_id, "name": name,
            "description": description, "phase": phase,
            "review_ignore_patterns": review_ignore_patterns,
            "created_by": created_by, "created_at": ts, "updated_at": ts,
        }


async def get_component(id: str) -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM components WHERE id = ?", (id,))
        if not rows:
            return None
        c = dict(rows[0])
        c.pop("secrets", None)
        c.pop("env_overrides", None)
        if c.get("review_ignore_patterns"):
            c["review_ignore_patterns"] = json.loads(c["review_ignore_patterns"])

        # Task summary + full task list
        task_rows = await db.execute_fetchall(
            """SELECT status, COUNT(*) as cnt, COALESCE(SUM(total_cost_usd), 0) as cost
               FROM tasks WHERE component_id = ? GROUP BY status""",
            (id,),
        )
        task_summary = {}
        total_tasks = 0
        total_cost = 0.0
        active_tasks = 0
        done_tasks = 0
        failed_tasks = 0
        for r in task_rows:
            task_summary[r["status"]] = r["cnt"]
            total_tasks += r["cnt"]
            total_cost += r["cost"]
            if r["status"] == "working":
                active_tasks = r["cnt"]
            if r["status"] in ("completed", "merged"):
                done_tasks += r["cnt"]
            if r["status"] == "failed":
                failed_tasks += r["cnt"]
        c["task_summary"] = {
            "by_status": task_summary,
            "total": total_tasks,
            "active": active_tasks,
            "total_cost": round(total_cost, 2),
        }
        # Flat fields for frontend progress bar
        c["total_tasks"] = total_tasks
        c["done_tasks"] = done_tasks
        c["active_tasks"] = active_tasks
        c["failed_tasks"] = failed_tasks
        c["total_cost"] = round(total_cost, 2)

        # Full task list for component detail view
        all_task_rows = await db.execute_fetchall(
            """SELECT t.*,
                (SELECT COUNT(*) FROM task_checklist WHERE task_id = t.id) as checklist_total,
                (SELECT COUNT(*) FROM task_checklist WHERE task_id = t.id AND done = TRUE) as checklist_done,
                (SELECT ref FROM task_artifacts WHERE task_id = t.id AND type = 'pr_url' LIMIT 1) as pr_url
               FROM tasks t WHERE t.component_id = ? ORDER BY t.created_at DESC""",
            (id,),
        )
        all_tasks = []
        for t in all_task_rows:
            task = dict(t)
            tag_rows = await db.execute_fetchall(
                "SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (task["id"],)
            )
            task["tags"] = [tr["tag"] for tr in tag_rows]
            all_tasks.append(task)
        # Find external dependencies — tasks in other components that our tasks depend on
        component_task_ids = {t["id"] for t in all_tasks}
        external_dep_ids = set()
        for t in all_tasks:
            dep = t.get("depends_on")
            if dep and dep not in component_task_ids:
                external_dep_ids.add(dep)

        for ext_id in external_dep_ids:
            ext_rows = await db.execute_fetchall(
                "SELECT t.*, c.name as component_name FROM tasks t LEFT JOIN components c ON t.component_id = c.id WHERE t.id = ?",
                (ext_id,),
            )
            if ext_rows:
                ext_task = dict(ext_rows[0])
                ext_task["_ghost"] = True  # marker for frontend
                ext_task["tags"] = []
                all_tasks.append(ext_task)

        c["tasks"] = all_tasks

        # Linked conversations (return objects with id + goal)
        conv_rows = await db.execute_fetchall(
            """SELECT cc.conversation_id AS id, conv.goal
               FROM component_conversations cc
               LEFT JOIN conversations conv ON conv.id = cc.conversation_id
               WHERE cc.component_id = ?""",
            (id,),
        )
        c["conversations"] = [dict(r) for r in conv_rows]

        return c


async def update_component(id: str, **fields) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM components WHERE id = ?", (id,))
        if not rows:
            raise ValueError(f"Component '{id}' not found")

        # Handle JSON field
        if "review_ignore_patterns" in fields:
            val = fields["review_ignore_patterns"]
            fields["review_ignore_patterns"] = json.dumps(val) if isinstance(val, list) else val

        # Filter to allowed fields
        fields = {k: v for k, v in fields.items() if k in COMPONENT_MUTABLE_FIELDS}
        fields["updated_at"] = now_iso()

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [id]
        await db.execute(f"UPDATE components SET {set_clause} WHERE id = ?", values)
        await db.commit()

        rows = await db.execute_fetchall("SELECT * FROM components WHERE id = ?", (id,))
        c = dict(rows[0])
        c.pop("secrets", None)
        c.pop("env_overrides", None)
        if c.get("review_ignore_patterns"):
            c["review_ignore_patterns"] = json.loads(c["review_ignore_patterns"])
        return c


async def list_components(project_id: str | None = None) -> list[dict]:
    async with get_db() as db:
        base_query = """SELECT c.*,
                    (SELECT COUNT(*) FROM tasks WHERE component_id = c.id) as total_tasks,
                    (SELECT COUNT(*) FROM tasks WHERE component_id = c.id AND status = 'working') as active_tasks,
                    (SELECT COUNT(*) FROM tasks WHERE component_id = c.id AND status IN ('completed', 'merged')) as done_tasks,
                    (SELECT COUNT(*) FROM tasks WHERE component_id = c.id AND status IN ('failed', 'needs-review')) as failed_tasks,
                    (SELECT COALESCE(SUM(total_cost_usd), 0) FROM tasks WHERE component_id = c.id) as total_cost,
                    (SELECT COUNT(*) FROM component_conversations WHERE component_id = c.id) as conversation_count,
                    (SELECT COUNT(*) FROM punchlist WHERE component_id = c.id AND status != 'done') as open_punchlist
                   FROM components c"""
        if project_id:
            rows = await db.execute_fetchall(
                base_query + " WHERE c.project_id = ? ORDER BY c.created_at DESC",
                (project_id,),
            )
        else:
            rows = await db.execute_fetchall(
                base_query + " ORDER BY c.created_at DESC"
            )
        results = []
        for r in rows:
            c = dict(r)
            c["total_cost"] = round(c.get("total_cost", 0), 2)
            c.pop("secrets", None)
            c.pop("env_overrides", None)
            if c.get("review_ignore_patterns"):
                c["review_ignore_patterns"] = json.loads(c["review_ignore_patterns"])
            results.append(c)
        return results


async def link_conversation(component_id: str, conversation_id: str) -> dict:
    async with get_db() as db:
        # Verify component exists
        rows = await db.execute_fetchall("SELECT id FROM components WHERE id = ?", (component_id,))
        if not rows:
            raise ValueError(f"Component '{component_id}' not found")
        await db.execute(
            "INSERT OR IGNORE INTO component_conversations (component_id, conversation_id) VALUES (?, ?)",
            (component_id, conversation_id),
        )
        await db.commit()
        return {"component_id": component_id, "conversation_id": conversation_id, "linked": True}


async def unlink_conversation(component_id: str, conversation_id: str) -> dict:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM component_conversations WHERE component_id = ? AND conversation_id = ?",
            (component_id, conversation_id),
        )
        await db.commit()
        return {"component_id": component_id, "conversation_id": conversation_id, "unlinked": True}


async def get_component_conversations(component_id: str) -> list[str]:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT conversation_id FROM component_conversations WHERE component_id = ?",
            (component_id,),
        )
        return [r["conversation_id"] for r in rows]


async def resolve_config(task_id: str) -> dict:
    """Resolve effective config for a task: task → project → system defaults.

    For scalar fields, returns the most-specific non-null value.
    For env_overrides, returns project-level value (tasks don't have env_overrides).
    """
    async with get_db() as db:
        task_rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not task_rows:
            raise ValueError(f"Task '{task_id}' not found")
        task = dict(task_rows[0])

        project_rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (task["project_id"],))
        project = dict(project_rows[0]) if project_rows else {}

    resolved = {}

    # Boolean fields that need normalization from SQLite 0/1
    bool_fields = {"auto_test", "auto_review", "auto_pr", "auto_merge", "auto_release_worktree"}

    # Scalar config fields: task → project → system default
    scalar_fields = [
        "base_branch", "model", "auto_test", "auto_review", "review_model",
        "max_test_retries", "max_review_retries", "auto_pr", "auto_merge",
        "auto_release_worktree",
        "setup_command", "test_command", "max_turns", "max_wall_clock",
    ]

    for field in scalar_fields:
        val = task.get(field)
        if val is None:
            val = project.get(field)
        if val is None:
            val = SYSTEM_DEFAULTS.get(field)
        # Normalize SQLite 0/1 to Python bool
        if field in bool_fields and val is not None:
            val = bool(val)
        resolved[field] = val

    # env_overrides: project-level only (tasks don't have their own env_overrides)
    pval = project.get("env_overrides")
    if isinstance(pval, str):
        pval = json.loads(pval)
    resolved["env_overrides"] = pval if pval else None

    return resolved
