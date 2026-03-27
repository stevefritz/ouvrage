"""Database operations for the files table."""
from switchboard.db.connection import get_db
from switchboard.db._helpers import now_iso


async def create_file(
    id: str,
    filename: str,
    stored_path: str,
    mime_type: str | None,
    size_bytes: int,
    uploaded_by: int | None,
) -> dict:
    ts = now_iso()
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO files (id, filename, stored_path, mime_type, size_bytes, uploaded_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (id, filename, stored_path, mime_type, size_bytes, uploaded_by, ts),
        )
        await conn.commit()
    return await get_file(id)


async def get_file(id: str) -> dict | None:
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT id, filename, stored_path, mime_type, size_bytes, uploaded_by, created_at, updated_at FROM files WHERE id = ?",
            (id,),
        )
    if not rows:
        return None
    return dict(rows[0])


async def list_files() -> list[dict]:
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT id, filename, stored_path, mime_type, size_bytes, uploaded_by, created_at, updated_at FROM files ORDER BY created_at DESC",
        )
    return [dict(r) for r in rows]


async def update_file(id: str, filename: str, stored_path: str, mime_type: str | None = None) -> dict | None:
    ts = now_iso()
    async with get_db() as conn:
        await conn.execute(
            "UPDATE files SET filename = ?, stored_path = ?, mime_type = ?, updated_at = ? WHERE id = ?",
            (filename, stored_path, mime_type, ts, id),
        )
        await conn.commit()
    return await get_file(id)


async def delete_file(id: str) -> bool:
    async with get_db() as conn:
        cursor = await conn.execute("DELETE FROM files WHERE id = ?", (id,))
        await conn.commit()
        return cursor.rowcount > 0
