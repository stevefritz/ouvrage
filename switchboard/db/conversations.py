"""Conversation and message CRUD."""
from switchboard.db.connection import get_db
from switchboard.db._helpers import now_iso, _strip_embedding, _read_messages, _list_with_aggregates


async def create_conversation(id: str, project: str, goal: str, claude_chat_url: str | None = None, created_by: int | None = None) -> dict:
    async with get_db() as db:
        ts = now_iso()
        await db.execute(
            "INSERT INTO conversations (id, project, goal, claude_chat_url, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (id, project, goal, claude_chat_url, created_by, ts, ts),
        )
        await db.commit()
        return {"id": id, "project": project, "goal": goal, "archived": False,
                "claude_chat_url": claude_chat_url, "created_by": created_by, "created_at": ts, "updated_at": ts}


async def post_message(conversation_id: str, author: str, content: str, type: str | None = None, title: str | None = None, pinned: bool = False, user_id: int | None = None) -> dict:
    async with get_db() as db:
        # Verify conversation exists
        row = await db.execute_fetchall("SELECT id FROM conversations WHERE id = ?", (conversation_id,))
        if not row:
            raise ValueError(f"Conversation '{conversation_id}' not found")

        # If pinning, unpin previous
        if pinned:
            await db.execute(
                "UPDATE messages SET pinned = FALSE WHERE conversation_id = ? AND pinned = TRUE",
                (conversation_id,),
            )

        ts = now_iso()
        cursor = await db.execute(
            "INSERT INTO messages (conversation_id, author, type, title, content, pinned, user_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, author, type, title, content, pinned, user_id, ts),
        )
        msg_id = cursor.lastrowid

        await db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (ts, conversation_id),
        )
        await db.commit()
        return {"id": msg_id, "conversation_id": conversation_id, "author": author, "type": type, "title": title, "content": content, "pinned": pinned, "user_id": user_id, "created_at": ts}


async def read_messages(conversation_id: str, last_n: int | None = None, since: str | None = None, after: int | None = None, author: str | None = None, type: str | None = None) -> dict:
    async with get_db() as db:
        # Verify conversation exists
        row = await db.execute_fetchall("SELECT id FROM conversations WHERE id = ?", (conversation_id,))
        if not row:
            raise ValueError(f"Conversation '{conversation_id}' not found")

    return await _read_messages(
        filter_column="conversation_id", filter_value=conversation_id,
        last_n=last_n, since=since, after=after, author=author, type=type,
    )


async def get_pinned(conversation_id: str) -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE conversation_id = ? AND pinned = TRUE LIMIT 1",
            (conversation_id,),
        )
        return _strip_embedding(dict(rows[0])) if rows else None


async def pin_message(message_id: int) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM messages WHERE id = ?", (message_id,))
        if not rows:
            raise ValueError(f"Message {message_id} not found")

        msg = dict(rows[0])
        conv_id = msg["conversation_id"]

        # Unpin previous
        await db.execute(
            "UPDATE messages SET pinned = FALSE WHERE conversation_id = ? AND pinned = TRUE",
            (conv_id,),
        )
        # Pin this one
        await db.execute("UPDATE messages SET pinned = TRUE WHERE id = ?", (message_id,))
        await db.commit()

        msg["pinned"] = True
        return _strip_embedding(msg)


async def board(project: str | None = None, include_archived: bool = False) -> list[dict]:
    conditions = []
    params: list = []

    if not include_archived:
        conditions.append("c.archived = FALSE")
    if project:
        conditions.append("c.project = ?")
        params.append(project)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return await _list_with_aggregates(where, params)


async def list_conversations(project: str | None = None, search: str | None = None) -> list[dict]:
    conditions = []
    params: list = []

    if project:
        conditions.append("c.project = ?")
        params.append(project)
    if search:
        conditions.append("c.goal LIKE ?")
        params.append(f"%{search}%")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    return await _list_with_aggregates(where, params)


async def archive_conversation(conversation_id: str) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id FROM conversations WHERE id = ?", (conversation_id,))
        if not rows:
            raise ValueError(f"Conversation '{conversation_id}' not found")

        await db.execute("UPDATE conversations SET archived = TRUE WHERE id = ?", (conversation_id,))
        await db.commit()
        return {"conversation_id": conversation_id, "archived": True}
