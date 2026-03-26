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
) -> dict:
    async with get_db() as db:
        ts = now_iso()
        env_json = json.dumps(env_overrides) if env_overrides else None
        state_json = json.dumps(state_definitions) if state_definitions else None
        await db.execute(
            """INSERT INTO projects
               (id, repo, default_branch, working_dir, setup_command, teardown_command,
                test_command, env_overrides, max_turns, max_wall_clock, claude_md_path, model,
                state_definitions, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, repo, default_branch, working_dir, setup_command, teardown_command,
             test_command, env_json, max_turns, max_wall_clock, claude_md_path, model,
             state_json, ts),
        )
        await db.commit()
        return {
            "id": id, "repo": repo, "default_branch": default_branch,
            "working_dir": working_dir, "setup_command": setup_command,
            "teardown_command": teardown_command, "test_command": test_command,
            "env_overrides": env_overrides, "max_turns": max_turns,
            "max_wall_clock": max_wall_clock, "claude_md_path": claude_md_path,
            "model": model, "state_definitions": state_definitions,
            "created_at": ts,
        }


async def get_project(id: str) -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (id,))
        if not rows:
            return None
        p = dict(rows[0])
        if p.get("env_overrides"):
            p["env_overrides"] = json.loads(p["env_overrides"])
        if p.get("state_definitions"):
            p["state_definitions"] = json.loads(p["state_definitions"])
        return p


async def update_project(project_id: str, **fields) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not rows:
            raise ValueError(f"Project '{project_id}' not found")

        if "env_overrides" in fields and isinstance(fields["env_overrides"], dict):
            fields["env_overrides"] = json.dumps(fields["env_overrides"])
        if "state_definitions" in fields and isinstance(fields["state_definitions"], dict):
            fields["state_definitions"] = json.dumps(fields["state_definitions"])

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [project_id]
        await db.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", values)
        await db.commit()

        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (project_id,))
        p = dict(rows[0])
        if p.get("env_overrides"):
            p["env_overrides"] = json.loads(p["env_overrides"])
        if p.get("state_definitions"):
            p["state_definitions"] = json.loads(p["state_definitions"])
        return p


async def list_projects() -> list[dict]:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM projects ORDER BY created_at DESC")
        projects = []
        for r in rows:
            p = dict(r)
            if p.get("env_overrides"):
                p["env_overrides"] = json.loads(p["env_overrides"])
            if p.get("state_definitions"):
                p["state_definitions"] = json.loads(p["state_definitions"])
            projects.append(p)
        return projects
