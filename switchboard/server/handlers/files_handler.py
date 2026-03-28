"""MCP tool handlers for files."""
import switchboard.db as db


async def _handle_list_files(arguments: dict) -> dict:
    task_id = arguments.get("task_id") or None
    files = await db.list_files(task_id=task_id)
    return {"files": files}
