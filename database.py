import aiosqlite
import os
from datetime import datetime, timezone

DB_PATH = os.environ.get("SWITCHBOARD_DB", "./data/switchboard.db")


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                goal TEXT NOT NULL,
                archived BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                updated_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                author TEXT NOT NULL,
                type TEXT,
                title TEXT,
                content TEXT NOT NULL,
                pinned BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            );

            CREATE INDEX IF NOT EXISTS idx_conv_project ON conversations(project);
            CREATE INDEX IF NOT EXISTS idx_msg_conversation ON messages(conversation_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_msg_pinned ON messages(conversation_id, pinned) WHERE pinned = TRUE;
        """)
        await db.commit()
    finally:
        await db.close()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def create_conversation(id: str, project: str, goal: str) -> dict:
    db = await get_db()
    try:
        ts = now_iso()
        await db.execute(
            "INSERT INTO conversations (id, project, goal, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (id, project, goal, ts, ts),
        )
        await db.commit()
        return {"id": id, "project": project, "goal": goal, "archived": False, "created_at": ts, "updated_at": ts}
    finally:
        await db.close()


async def post_message(conversation_id: str, author: str, content: str, type: str | None = None, title: str | None = None, pinned: bool = False) -> dict:
    db = await get_db()
    try:
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
            "INSERT INTO messages (conversation_id, author, type, title, content, pinned, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conversation_id, author, type, title, content, pinned, ts),
        )
        msg_id = cursor.lastrowid

        await db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (ts, conversation_id),
        )
        await db.commit()
        return {"id": msg_id, "conversation_id": conversation_id, "author": author, "type": type, "title": title, "content": content, "pinned": pinned, "created_at": ts}
    finally:
        await db.close()


async def read_messages(conversation_id: str, last_n: int | None = None, since: str | None = None, after: int | None = None, author: str | None = None, type: str | None = None) -> dict:
    db = await get_db()
    try:
        # Verify conversation exists
        row = await db.execute_fetchall("SELECT id FROM conversations WHERE id = ?", (conversation_id,))
        if not row:
            raise ValueError(f"Conversation '{conversation_id}' not found")

        # Get pinned message first
        pinned_rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE conversation_id = ? AND pinned = TRUE",
            (conversation_id,),
        )
        pinned = [dict(r) for r in pinned_rows]
        pinned_ids = {m["id"] for m in pinned}

        # Build query for non-pinned messages
        conditions = ["conversation_id = ?", "pinned = FALSE"]
        params: list = [conversation_id]

        if after is not None:
            conditions.append("id > ?")
            params.append(after)
        if since:
            conditions.append("created_at > ?")
            params.append(since)
        if author:
            conditions.append("author = ?")
            params.append(author)
        if type:
            conditions.append("type = ?")
            params.append(type)

        where = " AND ".join(conditions)
        query = f"SELECT * FROM messages WHERE {where} ORDER BY created_at ASC"

        if last_n:
            query = f"SELECT * FROM (SELECT * FROM messages WHERE {where} ORDER BY created_at DESC LIMIT ?) ORDER BY created_at ASC"
            params.append(last_n)

        rows = await db.execute_fetchall(query, params)
        messages = [dict(r) for r in rows if r["id"] not in pinned_ids]

        # Mark pinned messages
        for m in pinned:
            m["_pinned_marker"] = True

        all_messages = pinned + messages
        # Cursor = highest message ID across all returned messages
        cursor = max((m["id"] for m in all_messages), default=after or 0)

        return {"messages": all_messages, "cursor": cursor}
    finally:
        await db.close()


async def get_pinned(conversation_id: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE conversation_id = ? AND pinned = TRUE LIMIT 1",
            (conversation_id,),
        )
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def pin_message(message_id: int) -> dict:
    db = await get_db()
    try:
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
        return msg
    finally:
        await db.close()


async def board(project: str | None = None, include_archived: bool = False) -> list[dict]:
    db = await get_db()
    try:
        conditions = []
        params: list = []

        if not include_archived:
            conditions.append("c.archived = FALSE")
        if project:
            conditions.append("c.project = ?")
            params.append(project)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        query = f"""
            SELECT
                c.id, c.project, c.goal, c.archived, c.created_at, c.updated_at,
                COUNT(m.id) as message_count,
                (SELECT author FROM messages WHERE conversation_id = c.id ORDER BY created_at DESC LIMIT 1) as last_message_author,
                (SELECT title FROM messages WHERE conversation_id = c.id ORDER BY created_at DESC LIMIT 1) as last_message_title,
                (SELECT created_at FROM messages WHERE conversation_id = c.id ORDER BY created_at DESC LIMIT 1) as last_message_at,
                EXISTS(SELECT 1 FROM messages WHERE conversation_id = c.id AND pinned = TRUE) as has_pinned
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            {where}
            GROUP BY c.id
            ORDER BY c.updated_at DESC
        """
        rows = await db.execute_fetchall(query, params)
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def list_conversations(project: str | None = None, search: str | None = None) -> list[dict]:
    db = await get_db()
    try:
        conditions = []
        params: list = []

        if project:
            conditions.append("c.project = ?")
            params.append(project)
        if search:
            conditions.append("c.goal LIKE ?")
            params.append(f"%{search}%")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        query = f"""
            SELECT
                c.id, c.project, c.goal, c.archived, c.created_at, c.updated_at,
                COUNT(m.id) as message_count,
                (SELECT author FROM messages WHERE conversation_id = c.id ORDER BY created_at DESC LIMIT 1) as last_message_author,
                (SELECT title FROM messages WHERE conversation_id = c.id ORDER BY created_at DESC LIMIT 1) as last_message_title,
                (SELECT created_at FROM messages WHERE conversation_id = c.id ORDER BY created_at DESC LIMIT 1) as last_message_at,
                EXISTS(SELECT 1 FROM messages WHERE conversation_id = c.id AND pinned = TRUE) as has_pinned
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            {where}
            GROUP BY c.id
            ORDER BY c.updated_at DESC
        """
        rows = await db.execute_fetchall(query, params)
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def archive_conversation(conversation_id: str) -> dict:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM conversations WHERE id = ?", (conversation_id,))
        if not rows:
            raise ValueError(f"Conversation '{conversation_id}' not found")

        await db.execute("UPDATE conversations SET archived = TRUE WHERE id = ?", (conversation_id,))
        await db.commit()
        return {"conversation_id": conversation_id, "archived": True}
    finally:
        await db.close()
