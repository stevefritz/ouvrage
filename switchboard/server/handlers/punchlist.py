"""Punchlist tool handlers."""

import switchboard.db as db


async def _handle_add_punchlist_item(arguments):
    return await db.add_punchlist_item(
        component_id=arguments["component_id"],
        item=arguments["item"],
        author=arguments.get("author"),
    )


async def _handle_list_punchlist(arguments):
    return await db.list_punchlist(
        component_id=arguments["component_id"],
        include_done=arguments.get("include_done", False),
        claimed_by=arguments.get("claimed_by"),
    )


async def _handle_claim_punchlist_item(arguments):
    return await db.claim_punchlist_item(
        item_id=arguments["item_id"],
        task_id=arguments["task_id"],
    )


async def _handle_resolve_punchlist_item(arguments):
    item = await db.get_punchlist_item(arguments["item_id"])
    if not item:
        raise ValueError(f"Punchlist item {arguments['item_id']} not found")
    return await db.update_punchlist_item(
        item_id=arguments["item_id"],
        status="done",
        resolved_by=arguments["task_id"],
        resolved_at=db.now_iso(),
    )
