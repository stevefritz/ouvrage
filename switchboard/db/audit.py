"""Task audit log — records every task status transition for debugging."""

from switchboard.db.connection import get_db
from switchboard.db._helpers import now_iso


async def write_audit_log(
    task_id: str,
    action: str,
    triggered_by: str,
    source_detail: str | None = None,
    previous_status: str | None = None,
    new_status: str | None = None,
) -> dict:
    """Write an audit log entry for a task state transition."""
    async with get_db() as conn:
        ts = now_iso()
        cursor = await conn.execute(
            """INSERT INTO task_audit_log
               (task_id, action, triggered_by, source_detail,
                previous_status, new_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, action, triggered_by, source_detail,
             previous_status, new_status, ts),
        )
        await conn.commit()
        return {
            "id": cursor.lastrowid,
            "task_id": task_id,
            "action": action,
            "triggered_by": triggered_by,
            "source_detail": source_detail,
            "previous_status": previous_status,
            "new_status": new_status,
            "created_at": ts,
        }


async def get_audit_log(task_id: str) -> list[dict]:
    """Get all audit log entries for a task, ordered chronologically."""
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            """SELECT * FROM task_audit_log
               WHERE task_id = ? ORDER BY created_at ASC, id ASC""",
            (task_id,),
        )
        return [dict(r) for r in rows]
