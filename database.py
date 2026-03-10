import aiosqlite
import json
import os
from datetime import datetime, timezone

DB_PATH = os.environ.get("SWITCHBOARD_DB", "./data/switchboard.db")

# Global defaults for task resource limits
DEFAULT_MAX_TURNS = 200
DEFAULT_MAX_WALL_CLOCK = 60  # minutes
DEFAULT_MAX_CONCURRENT = 3


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    conn = await get_db()
    try:
        # Create new tables (won't affect existing ones)
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                goal TEXT NOT NULL,
                archived BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                updated_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                default_branch TEXT NOT NULL DEFAULT 'main',
                working_dir TEXT NOT NULL,
                setup_command TEXT,
                teardown_command TEXT,
                test_command TEXT,
                env_overrides TEXT,
                max_turns INTEGER,
                max_wall_clock INTEGER,
                claude_md_path TEXT,
                created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                goal TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ready',
                phase TEXT,
                branch TEXT,
                worktree_path TEXT,
                session_id TEXT,
                pid INTEGER,
                max_turns INTEGER,
                max_wall_clock INTEGER,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                total_cost_usd REAL DEFAULT 0.0,
                dispatch_count INTEGER DEFAULT 0,
                last_activity TIMESTAMP,
                created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                updated_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );

            CREATE TABLE IF NOT EXISTS task_checklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                item TEXT NOT NULL,
                done BOOLEAN DEFAULT FALSE,
                updated_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            );

            CREATE TABLE IF NOT EXISTS task_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                type TEXT NOT NULL,
                ref TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            );
        """)

        # Migrate messages table: add task_id column if missing
        columns = await conn.execute_fetchall("PRAGMA table_info(messages)")
        col_names = [c["name"] for c in columns]

        if "task_id" not in col_names:
            # Need to rebuild messages table to make conversation_id nullable + add task_id
            await conn.executescript("""
                CREATE TABLE IF NOT EXISTS messages_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT,
                    task_id TEXT,
                    author TEXT NOT NULL,
                    type TEXT,
                    title TEXT,
                    content TEXT NOT NULL,
                    pinned BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id),
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                );

                INSERT INTO messages_new (id, conversation_id, author, type, title, content, pinned, created_at)
                    SELECT id, conversation_id, author, type, title, content, pinned, created_at FROM messages;

                DROP TABLE messages;
                ALTER TABLE messages_new RENAME TO messages;
            """)
        elif "messages" not in [t["name"] for t in await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        )]:
            # Fresh install — create messages table
            await conn.executescript("""
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT,
                    task_id TEXT,
                    author TEXT NOT NULL,
                    type TEXT,
                    title TEXT,
                    content TEXT NOT NULL,
                    pinned BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id),
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                );
            """)

        # Create/recreate indexes
        await conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_conv_project ON conversations(project);
            CREATE INDEX IF NOT EXISTS idx_msg_conversation ON messages(conversation_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_msg_task ON messages(task_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_msg_pinned ON messages(conversation_id, pinned) WHERE pinned = TRUE;
            CREATE INDEX IF NOT EXISTS idx_msg_task_pinned ON messages(task_id, pinned) WHERE pinned = TRUE;
            CREATE INDEX IF NOT EXISTS idx_task_project ON tasks(project_id);
            CREATE INDEX IF NOT EXISTS idx_task_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_checklist_task ON task_checklist(task_id);
            CREATE INDEX IF NOT EXISTS idx_artifact_task ON task_artifacts(task_id);
        """)

        await conn.commit()
    finally:
        await conn.close()


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


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

async def create_project(
    id: str, repo: str, working_dir: str, default_branch: str = "main",
    setup_command: str | None = None, teardown_command: str | None = None,
    test_command: str | None = None, env_overrides: dict | None = None,
    max_turns: int | None = None, max_wall_clock: int | None = None,
    claude_md_path: str | None = None,
) -> dict:
    db = await get_db()
    try:
        ts = now_iso()
        env_json = json.dumps(env_overrides) if env_overrides else None
        await db.execute(
            """INSERT INTO projects
               (id, repo, default_branch, working_dir, setup_command, teardown_command,
                test_command, env_overrides, max_turns, max_wall_clock, claude_md_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, repo, default_branch, working_dir, setup_command, teardown_command,
             test_command, env_json, max_turns, max_wall_clock, claude_md_path, ts),
        )
        await db.commit()
        return {
            "id": id, "repo": repo, "default_branch": default_branch,
            "working_dir": working_dir, "setup_command": setup_command,
            "teardown_command": teardown_command, "test_command": test_command,
            "env_overrides": env_overrides, "max_turns": max_turns,
            "max_wall_clock": max_wall_clock, "claude_md_path": claude_md_path,
            "created_at": ts,
        }
    finally:
        await db.close()


async def get_project(id: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (id,))
        if not rows:
            return None
        p = dict(rows[0])
        if p.get("env_overrides"):
            p["env_overrides"] = json.loads(p["env_overrides"])
        return p
    finally:
        await db.close()


async def list_projects() -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM projects ORDER BY created_at DESC")
        projects = []
        for r in rows:
            p = dict(r)
            if p.get("env_overrides"):
                p["env_overrides"] = json.loads(p["env_overrides"])
            projects.append(p)
        return projects
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

async def create_task(
    id: str, project_id: str, goal: str, branch: str | None = None,
    max_turns: int | None = None, max_wall_clock: int | None = None,
) -> dict:
    db = await get_db()
    try:
        # Verify project exists
        rows = await db.execute_fetchall("SELECT id FROM projects WHERE id = ?", (project_id,))
        if not rows:
            raise ValueError(f"Project '{project_id}' not found")

        ts = now_iso()
        # If no explicit branch, derive from the short task ID (last segment)
        if not branch:
            branch = id.split("/")[-1]
        await db.execute(
            """INSERT INTO tasks
               (id, project_id, goal, status, branch, max_turns, max_wall_clock, created_at, updated_at)
               VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?)""",
            (id, project_id, goal, branch, max_turns, max_wall_clock, ts, ts),
        )
        await db.commit()
        return {
            "id": id, "project_id": project_id, "goal": goal, "status": "ready",
            "phase": None, "branch": branch, "worktree_path": None,
            "max_turns": max_turns, "max_wall_clock": max_wall_clock,
            "created_at": ts, "updated_at": ts,
        }
    finally:
        await db.close()


async def get_task(id: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (id,))
        if not rows:
            return None
        return dict(rows[0])
    finally:
        await db.close()


async def update_task(id: str, **fields) -> dict:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (id,))
        if not rows:
            raise ValueError(f"Task '{id}' not found")

        fields["updated_at"] = now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [id]
        await db.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        await db.commit()

        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (id,))
        return dict(rows[0])
    finally:
        await db.close()


async def list_tasks(project_id: str | None = None, status: str | None = None) -> list[dict]:
    db = await get_db()
    try:
        conditions = []
        params: list = []

        if project_id:
            conditions.append("t.project_id = ?")
            params.append(project_id)
        if status:
            conditions.append("t.status = ?")
            params.append(status)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        query = f"""
            SELECT t.*,
                (SELECT COUNT(*) FROM task_checklist WHERE task_id = t.id) as checklist_total,
                (SELECT COUNT(*) FROM task_checklist WHERE task_id = t.id AND done = TRUE) as checklist_done
            FROM tasks t
            {where}
            ORDER BY t.updated_at DESC
        """
        rows = await db.execute_fetchall(query, params)
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def count_active_tasks() -> int:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status = 'working'"
        )
        return rows[0]["cnt"]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Task Messages (reuses message model with task_id)
# ---------------------------------------------------------------------------

async def post_task_message(
    task_id: str, author: str, content: str,
    type: str | None = None, title: str | None = None, pinned: bool = False,
) -> dict:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

        if pinned:
            await db.execute(
                "UPDATE messages SET pinned = FALSE WHERE task_id = ? AND pinned = TRUE",
                (task_id,),
            )

        ts = now_iso()
        cursor = await db.execute(
            """INSERT INTO messages (task_id, author, type, title, content, pinned, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (task_id, author, type, title, content, pinned, ts),
        )
        msg_id = cursor.lastrowid

        await db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (ts, task_id))
        await db.commit()
        return {
            "id": msg_id, "task_id": task_id, "author": author,
            "type": type, "title": title, "content": content,
            "pinned": pinned, "created_at": ts,
        }
    finally:
        await db.close()


async def read_task_messages(
    task_id: str, last_n: int | None = None, after: int | None = None,
    type: str | None = None,
) -> dict:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

        # Get pinned message
        pinned_rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE task_id = ? AND pinned = TRUE", (task_id,),
        )
        pinned = [dict(r) for r in pinned_rows]
        pinned_ids = {m["id"] for m in pinned}

        conditions = ["task_id = ?", "pinned = FALSE"]
        params: list = [task_id]

        if after is not None:
            conditions.append("id > ?")
            params.append(after)
        if type:
            conditions.append("type = ?")
            params.append(type)

        where_clause = " AND ".join(conditions)
        query = f"SELECT * FROM messages WHERE {where_clause} ORDER BY created_at ASC"

        if last_n:
            query = f"SELECT * FROM (SELECT * FROM messages WHERE {where_clause} ORDER BY created_at DESC LIMIT ?) ORDER BY created_at ASC"
            params.append(last_n)

        rows = await db.execute_fetchall(query, params)
        messages = [dict(r) for r in rows if r["id"] not in pinned_ids]

        for m in pinned:
            m["_pinned_marker"] = True

        all_messages = pinned + messages
        cursor_val = max((m["id"] for m in all_messages), default=after or 0)
        return {"messages": all_messages, "cursor": cursor_val}
    finally:
        await db.close()


async def get_task_pinned(task_id: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE task_id = ? AND pinned = TRUE LIMIT 1",
            (task_id,),
        )
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Task Checklist
# ---------------------------------------------------------------------------

async def create_checklist_items(task_id: str, items: list[str]) -> list[dict]:
    db = await get_db()
    try:
        ts = now_iso()
        result = []
        for item in items:
            cursor = await db.execute(
                "INSERT INTO task_checklist (task_id, item, done, updated_at) VALUES (?, ?, FALSE, ?)",
                (task_id, item, ts),
            )
            result.append({"id": cursor.lastrowid, "task_id": task_id, "item": item, "done": False})
        await db.commit()
        return result
    finally:
        await db.close()


async def get_checklist(task_id: str) -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM task_checklist WHERE task_id = ? ORDER BY id", (task_id,),
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def update_checklist_item(item_id: int, done: bool) -> dict:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM task_checklist WHERE id = ?", (item_id,))
        if not rows:
            raise ValueError(f"Checklist item {item_id} not found")

        ts = now_iso()
        await db.execute(
            "UPDATE task_checklist SET done = ?, updated_at = ? WHERE id = ?",
            (done, ts, item_id),
        )
        await db.commit()
        item = dict(rows[0])
        item["done"] = done
        item["updated_at"] = ts
        return item
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Task Artifacts
# ---------------------------------------------------------------------------

async def add_artifact(task_id: str, type: str, ref: str) -> dict:
    db = await get_db()
    try:
        ts = now_iso()
        cursor = await db.execute(
            "INSERT INTO task_artifacts (task_id, type, ref, created_at) VALUES (?, ?, ?, ?)",
            (task_id, type, ref, ts),
        )
        await db.commit()
        return {"id": cursor.lastrowid, "task_id": task_id, "type": type, "ref": ref, "created_at": ts}
    finally:
        await db.close()


async def get_artifacts(task_id: str) -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY created_at", (task_id,),
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Task Status (rich)
# ---------------------------------------------------------------------------

async def get_task_status(task_id: str) -> dict:
    """Get comprehensive task status including checklist, messages, artifacts."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

        task = dict(rows[0])

        # Checklist summary
        cl_rows = await db.execute_fetchall(
            "SELECT * FROM task_checklist WHERE task_id = ? ORDER BY id", (task_id,),
        )
        checklist = [dict(r) for r in cl_rows]
        task["checklist"] = checklist
        task["checklist_total"] = len(checklist)
        task["checklist_done"] = sum(1 for c in checklist if c["done"])

        # Recent messages (last 5)
        msg_rows = await db.execute_fetchall(
            """SELECT * FROM messages WHERE task_id = ?
               ORDER BY created_at DESC LIMIT 5""",
            (task_id,),
        )
        task["recent_messages"] = [dict(r) for r in reversed(msg_rows)]

        # Artifacts
        art_rows = await db.execute_fetchall(
            "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        )
        task["artifacts"] = [dict(r) for r in art_rows]

        return task
    finally:
        await db.close()
