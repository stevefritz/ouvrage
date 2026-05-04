"""Database operations for the files table."""
from ouvrage.db.connection import get_db
from ouvrage.db._helpers import now_iso


async def create_file(
    id: str,
    filename: str,
    stored_path: str,
    mime_type: str | None,
    size_bytes: int,
    uploaded_by: int | None,
    task_id: str | None = None,
    project_id: str | None = None,
    role: str = "upload",
) -> dict:
    ts = now_iso()
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO files (id, filename, stored_path, mime_type, size_bytes, task_id, project_id, uploaded_by, role, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, filename, stored_path, mime_type, size_bytes, task_id, project_id, uploaded_by, role, ts),
        )
        await conn.commit()
    return await get_file(id)


async def get_file(id: str) -> dict | None:
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT id, filename, stored_path, mime_type, size_bytes, task_id, project_id, uploaded_by, created_at, updated_at FROM files WHERE id = ?",
            (id,),
        )
    if not rows:
        return None
    return dict(rows[0])


async def list_files(task_id: str | None = None, project_id: str | None = None) -> list[dict]:
    async with get_db() as conn:
        if task_id is not None:
            rows = await conn.execute_fetchall(
                "SELECT id, filename, stored_path, mime_type, size_bytes, task_id, project_id, uploaded_by, created_at, updated_at FROM files WHERE task_id = ? ORDER BY created_at DESC",
                (task_id,),
            )
        elif project_id is not None:
            rows = await conn.execute_fetchall(
                "SELECT id, filename, stored_path, mime_type, size_bytes, task_id, project_id, uploaded_by, created_at, updated_at FROM files WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            )
        else:
            rows = await conn.execute_fetchall(
                "SELECT id, filename, stored_path, mime_type, size_bytes, task_id, project_id, uploaded_by, created_at, updated_at FROM files ORDER BY created_at DESC",
            )
    return [dict(r) for r in rows]


async def promote_task_file(file_id: str, project_id: str) -> dict | None:
    """Set project_id on a task file, making it appear in both task and project file listings."""
    ts = now_iso()
    async with get_db() as conn:
        cursor = await conn.execute(
            "UPDATE files SET project_id = ?, updated_at = ? WHERE id = ? AND task_id IS NOT NULL",
            (project_id, ts, file_id),
        )
        await conn.commit()
        if cursor.rowcount == 0:
            return None
    return await get_file(file_id)


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
        rows = await conn.execute_fetchall(
            "SELECT role FROM files WHERE id = ?", (id,))
        if rows and rows[0]["role"] == "reference_doc":
            raise ValueError(
                f"File {id} is a reference doc and cannot be deleted directly. "
                "Use delete_reference_doc_config(...) which cascades correctly."
            )
        cursor = await conn.execute("DELETE FROM files WHERE id = ?", (id,))
        await conn.commit()
        return cursor.rowcount > 0


async def delete_reference_doc_files(file_id: str) -> bool:
    """Bypass the role guard. ONLY for service-driven cascade.
    Cascades through files_embeddings, file_chunks, files_vec, file_chunks_vec
    via FK ON DELETE CASCADE and the vec0 delete triggers."""
    async with get_db() as conn:
        cursor = await conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        await conn.commit()
        return cursor.rowcount > 0
