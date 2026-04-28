"""DB helpers for reference_doc_configs and reference_doc_runs tables."""
import json
import uuid

from ouvrage.db.connection import get_db
from ouvrage.db._helpers import now_iso


# ---------------------------------------------------------------------------
# Configs CRUD
# ---------------------------------------------------------------------------


async def upsert_config(
    *,
    project_id: str,
    slug: str,
    title: str,
    brief: str,
    source_hints: str | None = None,
    created_by: int | None = None,
) -> dict:
    """Upsert by (project_id, slug). Generates UUID id on insert.
    Updates updated_at on conflict. Returns the row.
    """
    ts = now_iso()
    new_id = str(uuid.uuid4())
    async with get_db() as conn:
        await conn.execute(
            """
            INSERT INTO reference_doc_configs
                (id, project_id, slug, title, brief, source_hints, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, slug) DO UPDATE SET
                title        = excluded.title,
                brief        = excluded.brief,
                source_hints = excluded.source_hints,
                updated_at   = excluded.updated_at
            """,
            (new_id, project_id, slug, title, brief, source_hints, created_by, ts, ts),
        )
        await conn.commit()
        rows = await conn.execute_fetchall(
            "SELECT * FROM reference_doc_configs WHERE project_id = ? AND slug = ?",
            (project_id, slug),
        )
    return dict(rows[0])


async def get_config(project_id: str, slug: str) -> dict | None:
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM reference_doc_configs WHERE project_id = ? AND slug = ?",
            (project_id, slug),
        )
    return dict(rows[0]) if rows else None


async def get_config_by_id(id: str) -> dict | None:
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM reference_doc_configs WHERE id = ?",
            (id,),
        )
    return dict(rows[0]) if rows else None


async def list_configs(project_id: str) -> list[dict]:
    """Ordered by slug ASC."""
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM reference_doc_configs WHERE project_id = ? ORDER BY slug ASC",
            (project_id,),
        )
    return [dict(r) for r in rows]


async def delete_config_row(id: str) -> bool:
    """Raw row delete. Caller is responsible for cascade (files row + cache + embeddings).
    The FK from reference_doc_runs to tasks does NOT need to be touched here —
    runs are append-only audit and may reference deleted configs by slug indirectly.
    """
    async with get_db() as conn:
        cursor = await conn.execute(
            "DELETE FROM reference_doc_configs WHERE id = ?", (id,)
        )
        await conn.commit()
    return cursor.rowcount > 0


async def update_config_meta(
    id: str,
    *,
    last_seen_sha: str | None = None,
    last_regen_at: str | None = None,
    last_regen_task_id: str | None = None,
) -> None:
    """Partial update of regen metadata. None values mean 'do not update this field'."""
    updates: dict[str, str] = {}
    if last_seen_sha is not None:
        updates["last_seen_sha"] = last_seen_sha
    if last_regen_at is not None:
        updates["last_regen_at"] = last_regen_at
    if last_regen_task_id is not None:
        updates["last_regen_task_id"] = last_regen_task_id
    if not updates:
        return
    updates["updated_at"] = now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [id]
    async with get_db() as conn:
        await conn.execute(
            f"UPDATE reference_doc_configs SET {set_clause} WHERE id = ?", values
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Runs (append-only)
# ---------------------------------------------------------------------------


def _decode_run(row: dict) -> dict:
    """JSON-decode slugs_changed and slugs_unchanged back to Python lists."""
    for field in ("slugs_changed", "slugs_unchanged"):
        val = row.get(field)
        if isinstance(val, str):
            try:
                row[field] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                row[field] = []
    return row


async def insert_run(
    *,
    project_id: str,
    task_id: str,
    commit_sha: str | None,
    outcome: str,
    slugs_changed: list[str],
    slugs_unchanged: list[str],
    error_message: str | None = None,
) -> dict:
    """Insert one row. JSON-encode the slug arrays. Returns inserted row with id."""
    async with get_db() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO reference_doc_runs
                (project_id, task_id, commit_sha, outcome,
                 slugs_changed, slugs_unchanged, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                task_id,
                commit_sha,
                outcome,
                json.dumps(slugs_changed),
                json.dumps(slugs_unchanged),
                error_message,
            ),
        )
        row_id = cursor.lastrowid
        await conn.commit()
        rows = await conn.execute_fetchall(
            "SELECT * FROM reference_doc_runs WHERE id = ?", (row_id,)
        )
    return _decode_run(dict(rows[0]))


async def list_runs(project_id: str, limit: int = 20) -> list[dict]:
    """Most recent first. Decodes slugs_* JSON arrays back to lists."""
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            """SELECT * FROM reference_doc_runs
               WHERE project_id = ?
               ORDER BY ran_at DESC, id DESC
               LIMIT ?""",
            (project_id, limit),
        )
    return [_decode_run(dict(r)) for r in rows]


async def get_runs_by_task(task_id: str) -> list[dict]:
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            """SELECT * FROM reference_doc_runs
               WHERE task_id = ?
               ORDER BY ran_at DESC, id DESC""",
            (task_id,),
        )
    return [_decode_run(dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Sweep / staleness helpers
# ---------------------------------------------------------------------------


async def get_latest_regen_at(project_id: str) -> str | None:
    """MAX(last_regen_at) across all configs for a project. None if no configs or no runs yet."""
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT MAX(last_regen_at) AS latest FROM reference_doc_configs WHERE project_id = ?",
            (project_id,),
        )
    if not rows:
        return None
    return rows[0]["latest"]


async def has_inflight_tagged_task(project_id: str, tag: str) -> bool:
    """True if any task in this project with the given tag is in working/validating status.
    Used by the cron sweep to avoid stacking regens.
    """
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            """SELECT 1 FROM tasks t
               JOIN task_tags tt ON tt.task_id = t.id
               WHERE t.project_id = ?
                 AND tt.tag = ?
                 AND t.status IN ('working', 'validating')
               LIMIT 1""",
            (project_id, tag),
        )
    return len(rows) > 0
