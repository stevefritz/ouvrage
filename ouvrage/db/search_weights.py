"""Search weight CRUD — manual weight overrides for search result ranking."""

from ouvrage.db.connection import get_db
from ouvrage.db._helpers import now_iso

_VALID_ENTITY_TYPES = {"task", "message", "chunk"}
_WEIGHT_MIN = 0.0
_WEIGHT_MAX = 3.0


def _validate(entity_type: str, weight: float) -> None:
    if entity_type not in _VALID_ENTITY_TYPES:
        raise ValueError(
            f"Invalid entity_type {entity_type!r}. Must be one of: {sorted(_VALID_ENTITY_TYPES)}"
        )
    if not (_WEIGHT_MIN <= weight <= _WEIGHT_MAX):
        raise ValueError(
            f"weight {weight} out of range. Must be in [{_WEIGHT_MIN}, {_WEIGHT_MAX}]."
        )


async def set_weight(
    entity_type: str,
    entity_id: str,
    weight: float,
    reason: str | None = None,
    user_id: int | None = None,
) -> dict:
    """Upsert a weight for (entity_type, entity_id).

    If a row already exists, updates weight, reason, and updated_at in place.
    Returns the resulting row as a dict.
    """
    _validate(entity_type, weight)
    ts = now_iso()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO search_weights
                   (entity_type, entity_id, weight, reason, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                   weight     = excluded.weight,
                   reason     = excluded.reason,
                   updated_at = excluded.updated_at""",
            (entity_type, entity_id, weight, reason, user_id, ts, ts),
        )
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT * FROM search_weights WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        )
    return dict(rows[0])


async def remove_weight(entity_type: str, entity_id: str) -> None:
    """Delete the weight for (entity_type, entity_id). Idempotent — no error if absent."""
    async with get_db() as db:
        await db.execute(
            "DELETE FROM search_weights WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        )
        await db.commit()


async def get_weight(entity_type: str, entity_id: str) -> dict | None:
    """Return the weight row for (entity_type, entity_id), or None if absent."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM search_weights WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        )
    return dict(rows[0]) if rows else None


async def list_weights(entity_type: str | None = None) -> list[dict]:
    """Return all weight rows, optionally filtered by entity_type."""
    async with get_db() as db:
        if entity_type is not None:
            rows = await db.execute_fetchall(
                "SELECT * FROM search_weights WHERE entity_type = ? ORDER BY entity_type, entity_id",
                (entity_type,),
            )
        else:
            rows = await db.execute_fetchall(
                "SELECT * FROM search_weights ORDER BY entity_type, entity_id"
            )
    return [dict(r) for r in rows]
