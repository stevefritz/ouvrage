"""Project CRUD."""
import json

from switchboard.db.connection import get_db
from switchboard.db._helpers import now_iso


async def create_project(
    id: str, repo: str, working_dir: str, default_branch: str = "main",
    setup_command: str | None = None, teardown_command: str | None = None,
    test_command: str | None = None, env_overrides: dict | None = None,
    max_turns: int | None = None, max_wall_clock: int | None = None,
    claude_md_path: str | None = None, model: str | None = None,
    state_definitions: dict | None = None,
    review_model: str | None = None,
    review_ignore_patterns: list | None = None,
    auto_test: bool | None = None,
    auto_review: bool | None = None,
    auto_pr: bool | None = None,
    auto_merge: bool | None = None,
    created_by: int | None = None,
    github_pat_override: str | None = None,
) -> dict:
    async with get_db() as db:
        ts = now_iso()
        env_json = json.dumps(env_overrides) if env_overrides else None
        state_json = json.dumps(state_definitions) if state_definitions else None
        rip_json = json.dumps(review_ignore_patterns) if review_ignore_patterns else None
        await db.execute(
            """INSERT INTO projects
               (id, repo, default_branch, working_dir, setup_command, teardown_command,
                test_command, env_overrides, max_turns, max_wall_clock, claude_md_path, model,
                state_definitions, review_model, review_ignore_patterns,
                auto_test, auto_review, auto_pr, auto_merge, created_by, created_at,
                github_pat_override)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, repo, default_branch, working_dir, setup_command, teardown_command,
             test_command, env_json, max_turns, max_wall_clock, claude_md_path, model,
             state_json, review_model, rip_json,
             auto_test, auto_review, auto_pr, auto_merge, created_by, ts,
             github_pat_override),
        )
        await db.commit()
        return {
            "id": id, "repo": repo, "default_branch": default_branch,
            "working_dir": working_dir, "setup_command": setup_command,
            "teardown_command": teardown_command, "test_command": test_command,
            "env_overrides": env_overrides, "max_turns": max_turns,
            "max_wall_clock": max_wall_clock, "claude_md_path": claude_md_path,
            "model": model, "state_definitions": state_definitions,
            "review_model": review_model, "review_ignore_patterns": review_ignore_patterns,
            "auto_test": auto_test, "auto_review": auto_review,
            "auto_pr": auto_pr, "auto_merge": auto_merge,
            "created_by": created_by, "created_at": ts,
            "github_pat_override": github_pat_override,
        }


def _decode_project(p: dict) -> dict:
    """Decode JSON fields in a project row dict."""
    if p.get("env_overrides"):
        p["env_overrides"] = json.loads(p["env_overrides"])
    if p.get("state_definitions"):
        p["state_definitions"] = json.loads(p["state_definitions"])
    if p.get("review_ignore_patterns"):
        p["review_ignore_patterns"] = json.loads(p["review_ignore_patterns"])
    return p


async def get_project(id: str) -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (id,))
        if not rows:
            return None
        return _decode_project(dict(rows[0]))


async def update_project(project_id: str, **fields) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not rows:
            raise ValueError(f"Project '{project_id}' not found")

        if "env_overrides" in fields and isinstance(fields["env_overrides"], dict):
            fields["env_overrides"] = json.dumps(fields["env_overrides"])
        if "state_definitions" in fields and isinstance(fields["state_definitions"], dict):
            fields["state_definitions"] = json.dumps(fields["state_definitions"])
        if "review_ignore_patterns" in fields and isinstance(fields["review_ignore_patterns"], list):
            fields["review_ignore_patterns"] = json.dumps(fields["review_ignore_patterns"])
        # Empty string means "clear the override" — store as NULL
        if "github_pat_override" in fields and fields["github_pat_override"] == "":
            fields["github_pat_override"] = None

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [project_id]
        await db.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", values)
        await db.commit()

        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (project_id,))
        return _decode_project(dict(rows[0]))


async def list_projects() -> list[dict]:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM projects ORDER BY created_at DESC")
        return [_decode_project(dict(r)) for r in rows]


async def count_projects() -> int:
    """Return the total number of registered projects."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT COUNT(*) AS cnt FROM projects")
        return rows[0]["cnt"] if rows else 0


async def rename_project(old_id: str, new_id: str) -> dict:
    """Rename a project, cascading the ID change through all tables atomically.

    Raises ValueError if:
    - old project not found
    - new_id already exists
    - any tasks are in active states (working/dispatching/testing/reviewing)

    Returns the updated project dict.
    """
    import re
    if not re.match(r'^[a-z0-9][a-z0-9-]*$', new_id):
        raise ValueError(
            f"new_id '{new_id}' is invalid — must match ^[a-z0-9][a-z0-9-]*$ "
            "(lowercase letters, digits, hyphens; must start with letter or digit)"
        )

    async with get_db() as db:
        # Verify old project exists
        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (old_id,))
        if not rows:
            raise ValueError(f"Project '{old_id}' not found")
        old_project = dict(rows[0])

        # Verify new_id doesn't already exist
        existing = await db.execute_fetchall("SELECT id FROM projects WHERE id = ?", (new_id,))
        if existing:
            raise ValueError(f"Project '{new_id}' already exists")

        # Reject if any tasks are active
        active_rows = await db.execute_fetchall(
            """SELECT id FROM tasks
               WHERE project_id = ? AND status IN ('working', 'dispatching', 'testing', 'reviewing')""",
            (old_id,),
        )
        if active_rows:
            ids = [r["id"] for r in active_rows]
            raise ValueError(
                f"Cannot rename project '{old_id}' — {len(ids)} task(s) are active: "
                f"{', '.join(ids)}. Cancel or wait for them to finish first."
            )

        # Compute new working_dir (same parent, folder renamed to new_id)
        import os as _os
        old_working_dir = old_project.get("working_dir") or ""
        if old_working_dir:
            new_working_dir = _os.path.join(_os.path.dirname(old_working_dir), new_id)
        else:
            new_working_dir = old_working_dir

        n = len(old_id)

        # Disable FK checks for the multi-table cascade UPDATE
        await db.execute("PRAGMA foreign_keys = OFF")
        try:
            # 1. projects: id + working_dir
            if new_working_dir:
                await db.execute(
                    "UPDATE projects SET id = ?, working_dir = ? WHERE id = ?",
                    (new_id, new_working_dir, old_id),
                )
            else:
                await db.execute("UPDATE projects SET id = ? WHERE id = ?", (new_id, old_id))

            # 2. tasks: project_id, id (prefix), depends_on (prefix), parent_task_id (prefix),
            #           worktree_path (path prefix)
            await db.execute(
                "UPDATE tasks SET project_id = ? WHERE project_id = ?", (new_id, old_id)
            )
            await db.execute(
                "UPDATE tasks SET id = ? || substr(id, ? + 1) WHERE id LIKE ? || '/%'",
                (new_id, n, old_id),
            )
            await db.execute(
                "UPDATE tasks SET depends_on = ? || substr(depends_on, ? + 1)"
                " WHERE depends_on LIKE ? || '/%'",
                (new_id, n, old_id),
            )
            await db.execute(
                "UPDATE tasks SET parent_task_id = ? || substr(parent_task_id, ? + 1)"
                " WHERE parent_task_id LIKE ? || '/%'",
                (new_id, n, old_id),
            )
            if old_working_dir and new_working_dir:
                wn = len(old_working_dir)
                await db.execute(
                    "UPDATE tasks SET worktree_path = ? || substr(worktree_path, ? + 1)"
                    " WHERE project_id = ? AND worktree_path IS NOT NULL"
                    " AND worktree_path LIKE ? || '/%'",
                    (new_working_dir, wn, new_id, old_working_dir),
                )

            # 3. Child tables keyed by task_id (prefix replace)
            for table in ("task_checklist", "task_artifacts", "task_tags",
                          "messages", "files", "task_audit_log", "task_attempts"):
                await db.execute(
                    f"UPDATE {table} SET task_id = ? || substr(task_id, ? + 1)"
                    f" WHERE task_id LIKE ? || '/%'",
                    (new_id, n, old_id),
                )

            # 4. subtasks: task_id and id (both carry the task_id prefix)
            await db.execute(
                "UPDATE subtasks SET task_id = ? || substr(task_id, ? + 1)"
                " WHERE task_id LIKE ? || '/%'",
                (new_id, n, old_id),
            )
            await db.execute(
                "UPDATE subtasks SET id = ? || substr(id, ? + 1) WHERE id LIKE ? || '/%'",
                (new_id, n, old_id),
            )

            # 5. punchlist: claimed_by and resolved_by store task IDs
            await db.execute(
                "UPDATE punchlist SET claimed_by = ? || substr(claimed_by, ? + 1)"
                " WHERE claimed_by LIKE ? || '/%'",
                (new_id, n, old_id),
            )
            await db.execute(
                "UPDATE punchlist SET resolved_by = ? || substr(resolved_by, ? + 1)"
                " WHERE resolved_by LIKE ? || '/%'",
                (new_id, n, old_id),
            )

            # 6. components: project_id
            await db.execute(
                "UPDATE components SET project_id = ? WHERE project_id = ?", (new_id, old_id)
            )

            # 7. conversations: project column (TEXT, no FK but semantically tied)
            await db.execute(
                "UPDATE conversations SET project = ? WHERE project = ?", (new_id, old_id)
            )

            await db.commit()
        except Exception:
            await db.execute("PRAGMA foreign_keys = ON")
            raise

        # Re-enable FK enforcement
        await db.execute("PRAGMA foreign_keys = ON")

        # Verify no FK violations were introduced
        violations = await db.execute_fetchall("PRAGMA foreign_key_check")
        if violations:
            raise ValueError(f"FK integrity violation after rename: {violations}")

        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (new_id,))
        return _decode_project(dict(rows[0]))


async def delete_project(project_id: str) -> None:
    """Delete a project and all its child records from the database.

    Cascades through tasks (checklist, artifacts, tags, subtasks, messages,
    files) and components (punchlist, component_conversations).
    Does NOT remove files from disk — callers are responsible for cleanup.
    Raises ValueError if the project doesn't exist.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id FROM projects WHERE id = ?", (project_id,))
        if not rows:
            raise ValueError(f"Project '{project_id}' not found")

        # Collect task IDs for this project
        task_rows = await db.execute_fetchall(
            "SELECT id FROM tasks WHERE project_id = ?", (project_id,)
        )
        task_ids = [r["id"] for r in task_rows]

        if task_ids:
            placeholders = ",".join("?" * len(task_ids))
            await db.execute(f"DELETE FROM task_checklist WHERE task_id IN ({placeholders})", task_ids)
            await db.execute(f"DELETE FROM task_artifacts WHERE task_id IN ({placeholders})", task_ids)
            await db.execute(f"DELETE FROM task_tags WHERE task_id IN ({placeholders})", task_ids)
            await db.execute(f"DELETE FROM subtasks WHERE task_id IN ({placeholders})", task_ids)
            await db.execute(f"DELETE FROM files WHERE task_id IN ({placeholders})", task_ids)
            # messages with task_id (message_chunks cascade via ON DELETE CASCADE)
            await db.execute(f"DELETE FROM messages WHERE task_id IN ({placeholders})", task_ids)

        await db.execute("DELETE FROM tasks WHERE project_id = ?", (project_id,))

        # Collect component IDs for this project
        comp_rows = await db.execute_fetchall(
            "SELECT id FROM components WHERE project_id = ?", (project_id,)
        )
        comp_ids = [r["id"] for r in comp_rows]

        if comp_ids:
            placeholders = ",".join("?" * len(comp_ids))
            await db.execute(f"DELETE FROM component_conversations WHERE component_id IN ({placeholders})", comp_ids)
            await db.execute(f"DELETE FROM punchlist WHERE component_id IN ({placeholders})", comp_ids)

        await db.execute("DELETE FROM components WHERE project_id = ?", (project_id,))
        await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await db.commit()
