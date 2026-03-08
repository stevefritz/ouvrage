import asyncio
import json
import os

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool, TextContent

import database as db

server = Server("switchboard")

TOOLS = [
    Tool(
        name="board",
        description="Show active conversations across projects. The main dashboard view.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Filter to one project key"},
                "include_archived": {"type": "boolean", "description": "Include archived conversations", "default": False},
            },
        },
    ),
    Tool(
        name="create_conversation",
        description="Start a new conversation on the switchboard.",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Slug ID, e.g. carrier-oversized-rates"},
                "project": {"type": "string", "description": "Project key, e.g. ap-carrier"},
                "goal": {"type": "string", "description": "One-liner purpose of this conversation"},
                "author": {"type": "string", "description": "Author for optional initial message"},
                "content": {"type": "string", "description": "Content for optional initial message"},
                "type": {"type": "string", "description": "Type for optional initial message"},
                "title": {"type": "string", "description": "Title for optional initial message"},
            },
            "required": ["id", "project", "goal"],
        },
    ),
    Tool(
        name="post",
        description="Post a message to a conversation. Anyone, anytime.",
        inputSchema={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Which conversation to post to"},
                "author": {"type": "string", "description": "Who is posting, e.g. claude-ai, claude-code, or a human name"},
                "content": {"type": "string", "description": "Full markdown body"},
                "type": {"type": "string", "description": "Optional: spec, plan, question, answer, note, review, status"},
                "title": {"type": "string", "description": "Optional short subject line"},
                "pinned": {"type": "boolean", "description": "Pin this message (auto-unpins previous)", "default": False},
            },
            "required": ["conversation_id", "author", "content"],
        },
    ),
    Tool(
        name="read",
        description="Get messages from a conversation. Pinned message always shown at top. Returns a cursor for polling — pass it back as 'after' to get only new messages.",
        inputSchema={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Which conversation to read"},
                "after": {"type": "integer", "description": "Cursor: return only messages with id > this value. Use the cursor from a previous read response."},
                "last_n": {"type": "integer", "description": "Return only the N most recent messages"},
                "since": {"type": "string", "description": "ISO timestamp, return messages after this time"},
                "author": {"type": "string", "description": "Filter by author"},
                "type": {"type": "string", "description": "Filter by message type"},
            },
            "required": ["conversation_id"],
        },
    ),
    Tool(
        name="get_pinned",
        description="Get the current pinned (source-of-truth) message for a conversation.",
        inputSchema={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Which conversation"},
            },
            "required": ["conversation_id"],
        },
    ),
    Tool(
        name="pin",
        description="Pin a specific message by ID. Auto-unpins any previously pinned message.",
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {"type": "integer", "description": "The message ID to pin"},
            },
            "required": ["message_id"],
        },
    ),
    Tool(
        name="conversations",
        description="List conversations, optionally filtered by project or search term.",
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Filter to one project"},
                "search": {"type": "string", "description": "Text search across conversation goals"},
            },
        },
    ),
    Tool(
        name="archive",
        description="Soft-archive a resolved conversation. Won't appear on board by default.",
        inputSchema={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Which conversation to archive"},
            },
            "required": ["conversation_id"],
        },
    ),
]


@server.list_tools()
async def list_tools():
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "board":
            result = await db.board(
                project=arguments.get("project"),
                include_archived=arguments.get("include_archived", False),
            )
        elif name == "create_conversation":
            result = await db.create_conversation(
                id=arguments["id"],
                project=arguments["project"],
                goal=arguments["goal"],
            )
            # Handle optional initial message
            if arguments.get("content"):
                msg = await db.post_message(
                    conversation_id=arguments["id"],
                    author=arguments.get("author", "human"),
                    content=arguments["content"],
                    type=arguments.get("type"),
                    title=arguments.get("title"),
                )
                result["initial_message"] = msg
        elif name == "post":
            result = await db.post_message(
                conversation_id=arguments["conversation_id"],
                author=arguments["author"],
                content=arguments["content"],
                type=arguments.get("type"),
                title=arguments.get("title"),
                pinned=arguments.get("pinned", False),
            )
        elif name == "read":
            result = await db.read_messages(
                conversation_id=arguments["conversation_id"],
                after=arguments.get("after"),
                last_n=arguments.get("last_n"),
                since=arguments.get("since"),
                author=arguments.get("author"),
                type=arguments.get("type"),
            )
        elif name == "get_pinned":
            result = await db.get_pinned(arguments["conversation_id"])
            if result is None:
                result = {"message": "No pinned message in this conversation"}
        elif name == "pin":
            result = await db.pin_message(arguments["message_id"])
        elif name == "conversations":
            result = await db.list_conversations(
                project=arguments.get("project"),
                search=arguments.get("search"),
            )
        elif name == "archive":
            result = await db.archive_conversation(arguments["conversation_id"])
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

        return [TextContent(type="text", text=json.dumps(result, separators=(",", ":"), default=str))]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def main():
    await db.init_db()

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=False,
    )

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            # Handle ASGI lifespan events for session manager
            message = await receive()
            if message["type"] == "lifespan.startup":
                ctx = session_manager.run()
                await ctx.__aenter__()
                scope["state"] = {"session_manager_ctx": ctx}
                await send({"type": "lifespan.startup.complete"})
                message = await receive()
                if message["type"] == "lifespan.shutdown":
                    await ctx.__aexit__(None, None, None)
                    await send({"type": "lifespan.shutdown.complete"})
            return

        if scope["type"] != "http":
            return

        path = scope["path"]
        method = scope.get("method", "")

        if path == "/health" and method == "GET":
            await send({"type": "http.response.start", "status": 200, "headers": [[b"content-type", b"text/plain"]]})
            await send({"type": "http.response.body", "body": b"Switchboard OK"})
        elif path == "/mcp":
            await session_manager.handle_request(scope, receive, send)
        else:
            await send({"type": "http.response.start", "status": 404, "headers": [[b"content-type", b"text/plain"]]})
            await send({"type": "http.response.body", "body": b"Not Found"})

    port = int(os.environ.get("SWITCHBOARD_PORT", "8100"))

    import uvicorn
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    srv = uvicorn.Server(config)
    await srv.serve()


if __name__ == "__main__":
    asyncio.run(main())
