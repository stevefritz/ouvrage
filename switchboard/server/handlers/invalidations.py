"""Handler for the invalidate MCP tool."""

import switchboard.db as db


async def _handle_invalidate(arguments: dict) -> dict:
    """Soft-suppress or restore a search entity.

    strength == 0: remove invalidation (restore full weight).
    strength 0.1-1.0: upsert invalidation.
    """
    entity_type = arguments["entity_type"]
    entity_id = arguments["entity_id"]
    strength = float(arguments["strength"])
    reason = arguments.get("reason")

    if strength == 0:
        removed = await db.delete_invalidation(entity_type, entity_id)
        return {"entity_type": entity_type, "entity_id": entity_id, "removed": removed}

    record = await db.upsert_invalidation(entity_type, entity_id, strength, reason)
    return record
