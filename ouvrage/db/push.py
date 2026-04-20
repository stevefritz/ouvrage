"""Push subscriptions and notification settings."""
from ouvrage.db.connection import get_db
from ouvrage.db._helpers import now_iso


async def get_push_subscriptions() -> list[dict]:
    async with get_db() as conn:
        rows = await conn.execute_fetchall("SELECT * FROM push_subscriptions ORDER BY created_at")
        return [dict(r) for r in rows]


async def save_push_subscription(endpoint: str, p256dh: str, auth: str) -> dict:
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth""",
            (endpoint, p256dh, auth, now_iso()),
        )
        await conn.commit()
        row = await conn.execute_fetchall(
            "SELECT * FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
        )
        return dict(row[0]) if row else {}


async def delete_push_subscription(endpoint: str) -> bool:
    async with get_db() as conn:
        cursor = await conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
        )
        await conn.commit()
        return cursor.rowcount > 0


async def get_notification_settings() -> dict:
    async with get_db() as conn:
        rows = await conn.execute_fetchall("SELECT * FROM notification_settings WHERE id = 1")
        if rows:
            return dict(rows[0])
        return {
            "id": 1,
            "notify_failed": True,
            "notify_needs_review": True,
            "notify_completed": False,
            "notify_question": True,
        }


async def update_notification_settings(**kwargs) -> dict:
    allowed = {"notify_failed", "notify_needs_review", "notify_completed", "notify_question"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return await get_notification_settings()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    async with get_db() as conn:
        await conn.execute(
            f"UPDATE notification_settings SET {set_clause} WHERE id = 1",
            list(updates.values()),
        )
        await conn.commit()
    return await get_notification_settings()
