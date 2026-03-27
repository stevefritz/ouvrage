"""MCP tool handlers for files."""
import switchboard.db as db


async def _handle_list_files(arguments: dict) -> dict:
    files = await db.list_files()
    return {"files": files}
