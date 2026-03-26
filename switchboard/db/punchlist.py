"""Punchlist CRUD."""
from switchboard.db.connection import get_db
from switchboard.db._helpers import now_iso


async def add_punchlist_item(component_id: str, item: str, author: str | None = None) -> dict:
    """Add a punchlist item for a component. Raises ValueError if component not found."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id FROM components WHERE id = ?", (component_id,))
        if not rows:
            raise ValueError(f"Component '{component_id}' not found")
        ts = now_iso()
        cursor = await db.execute(
            """INSERT INTO punchlist (component_id, item, status, author, created_at)
               VALUES (?, ?, 'open', ?, ?)""",
            (component_id, item, author, ts),
        )
        await db.commit()
        return {
            "id": cursor.lastrowid,
            "component_id": component_id,
            "item": item,
            "status": "open",
            "claimed_by": None,
            "resolved_by": None,
            "resolved_at": None,
            "author": author,
            "created_at": ts,
        }


async def get_punchlist_item(item_id: int) -> dict | None:
    """Get a single punchlist item by ID."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM punchlist WHERE id = ?", (item_id,))
        if not rows:
            return None
        return dict(rows[0])


async def list_punchlist(
    component_id: str,
    include_done: bool = False,
    claimed_by: str | None = None,
) -> list[dict]:
    """List punchlist items for a component. Excludes 'done' by default."""
    async with get_db() as db:
        conditions = ["component_id = ?"]
        params: list = [component_id]
        if not include_done:
            conditions.append("status != 'done'")
        if claimed_by is not None:
            conditions.append("claimed_by = ?")
            params.append(claimed_by)
        where = " AND ".join(conditions)
        rows = await db.execute_fetchall(
            f"SELECT * FROM punchlist WHERE {where} ORDER BY id ASC", params
        )
        return [dict(r) for r in rows]


async def claim_punchlist_item(item_id: int, task_id: str) -> dict:
    """Claim a punchlist item for a task. Raises ValueError if not found or already done."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM punchlist WHERE id = ?", (item_id,))
        if not rows:
            raise ValueError(f"Punchlist item {item_id} not found")
        item = dict(rows[0])
        if item["status"] == "done":
            raise ValueError(f"Punchlist item {item_id} is already done")
        await db.execute(
            "UPDATE punchlist SET status = 'claimed', claimed_by = ? WHERE id = ?",
            (task_id, item_id),
        )
        await db.commit()
        item["status"] = "claimed"
        item["claimed_by"] = task_id
        return item


async def resolve_punchlist_items_for_task(task_id: str) -> int:
    """Mark all 'claimed' items for this task as 'done'. Returns count resolved."""
    async with get_db() as db:
        ts = now_iso()
        cursor = await db.execute(
            """UPDATE punchlist SET status = 'done', resolved_by = ?, resolved_at = ?
               WHERE claimed_by = ? AND status = 'claimed'""",
            (task_id, ts, task_id),
        )
        await db.commit()
        return cursor.rowcount


# Aliases used by dashboard REST API
async def create_punchlist_item(component_id: str, item: str) -> dict:
    """Alias for add_punchlist_item (used by dashboard API)."""
    return await add_punchlist_item(component_id, item)


async def update_punchlist_item(item_id: int, **fields) -> dict:
    """Update arbitrary fields on a punchlist item."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM punchlist WHERE id = ?", (item_id,))
        if not rows:
            raise ValueError(f"Punchlist item {item_id} not found")
        allowed = {"status", "claimed_by", "resolved_by", "resolved_at", "item"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            await db.execute(
                f"UPDATE punchlist SET {set_clause} WHERE id = ?",
                (*updates.values(), item_id),
            )
            await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM punchlist WHERE id = ?", (item_id,))
        return dict(rows[0])


async def delete_punchlist_item(item_id: int) -> bool:
    """Delete a punchlist item by ID."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM punchlist WHERE id = ?", (item_id,))
        await db.commit()
        return cursor.rowcount > 0


async def revert_punchlist_items_for_task(task_id: str) -> int:
    """Revert 'claimed' items for this task back to 'open'. Returns count reverted."""
    async with get_db() as db:
        cursor = await db.execute(
            """UPDATE punchlist SET status = 'open', claimed_by = NULL
               WHERE claimed_by = ? AND status = 'claimed'""",
            (task_id,),
        )
        await db.commit()
        return cursor.rowcount
