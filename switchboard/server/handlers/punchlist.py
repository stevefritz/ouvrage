"""Punchlist tool handlers."""

import json

from mcp.types import TextContent

import database as db


async def _handle_add_punchlist_item(arguments):
    result = await db.add_punchlist_item(
        component_id=arguments["component_id"],
        item=arguments["item"],
        author=arguments.get("author"),
    )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def _handle_list_punchlist(arguments):
    items = await db.list_punchlist(
        component_id=arguments["component_id"],
        include_done=arguments.get("include_done", False),
        claimed_by=arguments.get("claimed_by"),
    )
    return [TextContent(type="text", text=json.dumps(items, indent=2))]


async def _handle_claim_punchlist_item(arguments):
    result = await db.claim_punchlist_item(
        item_id=arguments["item_id"],
        task_id=arguments["task_id"],
    )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def _handle_resolve_punchlist_item(arguments):
    item = await db.get_punchlist_item(arguments["item_id"])
    if not item:
        raise ValueError(f"Punchlist item {arguments['item_id']} not found")
    result = await db.update_punchlist_item(
        item_id=arguments["item_id"],
        status="done",
        resolved_by=arguments["task_id"],
        resolved_at=db.now_iso(),
    )
    return [TextContent(type="text", text=json.dumps(result, indent=2))]
