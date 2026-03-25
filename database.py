import aiosqlite
import httpx
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("SWITCHBOARD_DB", "./data/switchboard.db")

# Core state definitions — hardcoded defaults for the dashboard
CORE_STATE_DEFINITIONS = {
    "ready":         {"color": "#6b7280", "label": "Ready",        "pulse": False},
    "blocked":       {"color": "#f59e0b", "label": "Blocked",      "pulse": False},
    "working":       {"color": "#3b82f6", "label": "Working",      "pulse": True},
    "testing":       {"color": "#8b5cf6", "label": "Testing",      "pulse": True},
    "reviewing":     {"color": "#8b5cf6", "label": "Reviewing",    "pulse": True},
    "needs-review":  {"color": "#f59e0b", "label": "Needs Review", "pulse": False},
    "turns-exhausted": {"color": "#f59e0b", "label": "Turns Exhausted", "pulse": False},
    "completed":     {"color": "#10b981", "label": "Completed",    "pulse": False},
    "merged":        {"color": "#10b981", "label": "Merged",       "pulse": False},
    "failed":        {"color": "#ef4444", "label": "Failed",       "pulse": False},
    "cancelled":     {"color": "#6b7280", "label": "Cancelled",    "pulse": False},
}

# Global defaults for task resource limits
DEFAULT_MAX_TURNS = 200
DEFAULT_MAX_WALL_CLOCK = 60  # minutes
DEFAULT_MAX_CONCURRENT = 6

# ---------------------------------------------------------------------------
# Connection Management — singleton with async context manager
# ---------------------------------------------------------------------------

_connection: aiosqlite.Connection | None = None


async def _get_shared_connection() -> aiosqlite.Connection:
    """Get or create the shared database connection. Sets PRAGMAs once."""
    global _connection
    if _connection is None:
        _connection = await aiosqlite.connect(DB_PATH)
        _connection.row_factory = aiosqlite.Row
        await _connection.execute("PRAGMA journal_mode=WAL")
        await _connection.execute("PRAGMA foreign_keys=ON")
    return _connection


@asynccontextmanager
async def get_db():
    """Async context manager that yields the shared connection."""
    db = await _get_shared_connection()
    yield db


async def close_db():
    """Close the shared connection. Call on shutdown."""
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None


async def init_db():
    async with get_db() as conn:
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

            CREATE TABLE IF NOT EXISTS task_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id),
                UNIQUE(task_id, tag)
            );

            CREATE TABLE IF NOT EXISTS subtasks (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'working',
                model TEXT DEFAULT 'opus',
                prompt TEXT,
                result TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0.0,
                duration_ms INTEGER,
                created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                completed_at TIMESTAMP,
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_subtask_task ON subtasks(task_id);

            CREATE TABLE IF NOT EXISTS components (
                id              TEXT PRIMARY KEY,
                project_id      TEXT NOT NULL,
                name            TEXT NOT NULL,
                description     TEXT,
                phase           TEXT DEFAULT 'planning',
                base_branch     TEXT,
                setup_command   TEXT,
                test_command    TEXT,
                model           TEXT,
                auto_test       BOOLEAN,
                auto_review     BOOLEAN,
                review_model    TEXT,
                max_test_retries INTEGER,
                max_review_retries INTEGER,
                auto_pr         BOOLEAN,
                auto_merge      BOOLEAN,
                max_turns       INTEGER,
                max_wall_clock  INTEGER,
                env_overrides   TEXT,
                secrets         TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );

            CREATE TABLE IF NOT EXISTS component_conversations (
                component_id    TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                PRIMARY KEY (component_id, conversation_id),
                FOREIGN KEY (component_id) REFERENCES components(id)
            );

            CREATE TABLE IF NOT EXISTS punchlist (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                component_id    TEXT NOT NULL,
                item            TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'open',
                claimed_by      TEXT,
                resolved_by     TEXT,
                resolved_at     TEXT,
                author          TEXT,
                created_at      TEXT NOT NULL,
                FOREIGN KEY (component_id) REFERENCES components(id)
            );

            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notification_settings (
                id INTEGER PRIMARY KEY,
                notify_failed BOOLEAN DEFAULT 1,
                notify_needs_review BOOLEAN DEFAULT 1,
                notify_completed BOOLEAN DEFAULT 0,
                notify_question BOOLEAN DEFAULT 1
            );

            INSERT OR IGNORE INTO notification_settings (id, notify_failed, notify_needs_review, notify_completed, notify_question)
                VALUES (1, 1, 1, 0, 1);
        """)

        # Migrate messages table: add task_id column if missing
        table_exists = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        )

        if not table_exists:
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
        else:
            columns = await conn.execute_fetchall("PRAGMA table_info(messages)")
            col_names = [c["name"] for c in columns]
            if "task_id" not in col_names:
                # Old schema — rebuild to add task_id and make conversation_id nullable
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

        # Migrate tasks table: add jira_ticket, conversation_id columns if missing
        task_columns = await conn.execute_fetchall("PRAGMA table_info(tasks)")
        task_col_names = [c["name"] for c in task_columns]
        if "pid" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN pid INTEGER")
        if "jira_ticket" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN jira_ticket TEXT")
        if "conversation_id" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN conversation_id TEXT")
        if "model" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN model TEXT")
        if "auto_test" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN auto_test BOOLEAN DEFAULT TRUE")
        if "gate_status" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN gate_status TEXT")
        if "gate_retries" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN gate_retries INTEGER DEFAULT 0")
        if "max_gate_retries" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN max_gate_retries INTEGER DEFAULT 3")
        if "gate_passed_at" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN gate_passed_at TIMESTAMP")
        if "depends_on" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN depends_on TEXT")
        if "auto_review" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN auto_review BOOLEAN DEFAULT TRUE")
        if "review_model" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN review_model TEXT DEFAULT 'opus'")
        if "parent_task_id" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN parent_task_id TEXT")
        if "auto_pr" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN auto_pr BOOLEAN DEFAULT FALSE")
        if "component_id" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN component_id TEXT")
        if "base_branch" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN base_branch TEXT")
        if "branch_target" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN branch_target TEXT")
        if "claude_chat_url" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN claude_chat_url TEXT")
        # v5-auto-merge-queue: FIFO queue + auto-merge fields
        if "queued_at" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN queued_at TEXT")
        if "auto_merge" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN auto_merge BOOLEAN")
        if "auto_release_worktree" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN auto_release_worktree BOOLEAN DEFAULT 1")
        if "pushed_at" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN pushed_at TEXT")
        if "pr_status" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN pr_status TEXT")
        if "pr_error" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN pr_error TEXT")
        # v5-migration-toolkit fields
        if "max_test_retries" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN max_test_retries INTEGER")
        if "max_review_retries" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN max_review_retries INTEGER")
        # v5-crash-recovery: flap detection + queue priority
        if "recovery_count" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN recovery_count INTEGER DEFAULT 0")
        if "last_recovery_at" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN last_recovery_at TEXT")
        if "recovery_priority" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN recovery_priority BOOLEAN DEFAULT 0")
        # v5-realtime-output: structured test output + attempt tracking
        if "last_test_output" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN last_test_output TEXT")
        if "current_attempt" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN current_attempt INTEGER DEFAULT 1")
        if "retry_after" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN retry_after TEXT")
        if "held" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN held BOOLEAN DEFAULT 0")

        # Migrate messages table: add attempt_number and embedding if missing
        msg_columns = await conn.execute_fetchall("PRAGMA table_info(messages)")
        msg_col_names = [c["name"] for c in msg_columns]
        if "attempt_number" not in msg_col_names:
            await conn.execute("ALTER TABLE messages ADD COLUMN attempt_number INTEGER DEFAULT 1")
        if "embedding" not in msg_col_names:
            await conn.execute("ALTER TABLE messages ADD COLUMN embedding BLOB")

        # Migrate conversations table: add claude_chat_url if missing
        conv_columns = await conn.execute_fetchall("PRAGMA table_info(conversations)")
        conv_col_names = [c["name"] for c in conv_columns]
        if "claude_chat_url" not in conv_col_names:
            await conn.execute("ALTER TABLE conversations ADD COLUMN claude_chat_url TEXT")

        # Migrate projects table: add model column if missing
        project_columns = await conn.execute_fetchall("PRAGMA table_info(projects)")
        project_col_names = [c["name"] for c in project_columns]
        if "model" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN model TEXT")
        if "connectors" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN connectors TEXT")
        if "state_definitions" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN state_definitions TEXT")
        if "review_ignore_patterns" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN review_ignore_patterns TEXT")

        # Migrate components table
        comp_table = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='components'"
        )
        if comp_table:
            comp_columns = await conn.execute_fetchall("PRAGMA table_info(components)")
            comp_col_names = [c["name"] for c in comp_columns]
            if "review_ignore_patterns" not in comp_col_names:
                await conn.execute("ALTER TABLE components ADD COLUMN review_ignore_patterns TEXT")
            if "paused" not in comp_col_names:
                await conn.execute("ALTER TABLE components ADD COLUMN paused BOOLEAN DEFAULT 0")

        # Migrate projects: add paused
        if "paused" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN paused BOOLEAN DEFAULT 0")

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
            CREATE INDEX IF NOT EXISTS idx_task_tags ON task_tags(task_id);
            CREATE INDEX IF NOT EXISTS idx_task_tags_tag ON task_tags(tag);
            CREATE INDEX IF NOT EXISTS idx_msg_content ON messages(content);
            CREATE INDEX IF NOT EXISTS idx_component_project ON components(project_id);
            CREATE INDEX IF NOT EXISTS idx_task_component ON tasks(component_id);
            CREATE INDEX IF NOT EXISTS idx_punchlist_component ON punchlist(component_id);
            CREATE INDEX IF NOT EXISTS idx_punchlist_claimed_by ON punchlist(claimed_by);
        """)

        await conn.commit()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Push subscriptions
# ---------------------------------------------------------------------------

async def get_push_subscriptions() -> list[dict]:
    async with get_db() as conn:
        rows = await conn.execute_fetchall("SELECT * FROM push_subscriptions ORDER BY created_at")
        return [dict(r) for r in rows]


async def save_push_subscription(endpoint: str, p256dh: str, auth: str) -> dict:
    async with get_db() as conn:
        await conn.execute(
            """INSERT INTO push_subscriptions (endpoint, p256dh, auth, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth""",
            (endpoint, p256dh, auth, now_iso()),
        )
        await conn.commit()
        row = await conn.execute_fetchall(
            "SELECT * FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
        )
        return dict(row[0]) if row else {}


async def delete_push_subscription(endpoint: str) -> bool:
    async with get_db() as conn:
        cursor = await conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
        )
        await conn.commit()
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Notification settings
# ---------------------------------------------------------------------------

async def get_notification_settings() -> dict:
    async with get_db() as conn:
        rows = await conn.execute_fetchall("SELECT * FROM notification_settings WHERE id = 1")
        if rows:
            return dict(rows[0])
        return {
            "id": 1,
            "notify_failed": True,
            "notify_needs_review": True,
            "notify_completed": False,
            "notify_question": True,
        }


async def update_notification_settings(**kwargs) -> dict:
    allowed = {"notify_failed", "notify_needs_review", "notify_completed", "notify_question"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return await get_notification_settings()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    async with get_db() as conn:
        await conn.execute(
            f"UPDATE notification_settings SET {set_clause} WHERE id = 1",
            list(updates.values()),
        )
        await conn.commit()
    return await get_notification_settings()


# ---------------------------------------------------------------------------
# Shared helpers — deduplicated read logic
# ---------------------------------------------------------------------------

def _strip_embedding(msg: dict) -> dict:
    """Remove the embedding field from a message dict before returning to callers.

    Embeddings are internal-only (semantic search). They're never useful to
    external callers and can be massive binary blobs. Strip defensively so that
    adding an embedding column later never leaks into API responses.
    """
    msg.pop("embedding", None)
    return msg


async def _read_messages(
    filter_column: str, filter_value: str,
    last_n: int | None = None, since: str | None = None,
    after: int | None = None, author: str | None = None,
    type: str | None = None,
) -> dict:
    """Shared implementation for read_messages() and read_task_messages()."""
    async with get_db() as db:
        # Get pinned messages first
        pinned_rows = await db.execute_fetchall(
            f"SELECT * FROM messages WHERE {filter_column} = ? AND pinned = TRUE",
            (filter_value,),
        )
        pinned = [_strip_embedding(dict(r)) for r in pinned_rows]
        pinned_ids = {m["id"] for m in pinned}

        # Build query for non-pinned messages
        conditions = [f"{filter_column} = ?", "pinned = FALSE"]
        params: list = [filter_value]

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
        messages = [_strip_embedding(dict(r)) for r in rows if r["id"] not in pinned_ids]

        # Mark pinned messages
        for m in pinned:
            m["_pinned_marker"] = True

        all_messages = pinned + messages
        cursor = max((m["id"] for m in all_messages), default=after or 0)

        return {"messages": all_messages, "cursor": cursor}


async def _list_with_aggregates(
    where_clause: str, params: list,
) -> list[dict]:
    """Shared implementation for board() and list_conversations().

    Uses a CTE with ROW_NUMBER() to get last message info in one pass
    instead of three correlated subqueries.
    """
    async with get_db() as db:
        query = f"""
            WITH latest_msg AS (
                SELECT conversation_id, author, title, created_at,
                       ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY created_at DESC) as rn
                FROM messages
                WHERE conversation_id IS NOT NULL
            )
            SELECT
                c.id, c.project, c.goal, c.archived, c.created_at, c.updated_at,
                COUNT(m.id) as message_count,
                lm.author as last_message_author,
                lm.title as last_message_title,
                lm.created_at as last_message_at,
                EXISTS(SELECT 1 FROM messages WHERE conversation_id = c.id AND pinned = TRUE) as has_pinned
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            LEFT JOIN latest_msg lm ON lm.conversation_id = c.id AND lm.rn = 1
            {where_clause}
            GROUP BY c.id
            ORDER BY c.updated_at DESC
        """
        rows = await db.execute_fetchall(query, params)
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

async def create_conversation(id: str, project: str, goal: str, claude_chat_url: str | None = None) -> dict:
    async with get_db() as db:
        ts = now_iso()
        await db.execute(
            "INSERT INTO conversations (id, project, goal, claude_chat_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (id, project, goal, claude_chat_url, ts, ts),
        )
        await db.commit()
        return {"id": id, "project": project, "goal": goal, "archived": False,
                "claude_chat_url": claude_chat_url, "created_at": ts, "updated_at": ts}


async def post_message(conversation_id: str, author: str, content: str, type: str | None = None, title: str | None = None, pinned: bool = False) -> dict:
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
        return msg


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


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

async def create_project(
    id: str, repo: str, working_dir: str, default_branch: str = "main",
    setup_command: str | None = None, teardown_command: str | None = None,
    test_command: str | None = None, env_overrides: dict | None = None,
    max_turns: int | None = None, max_wall_clock: int | None = None,
    claude_md_path: str | None = None, model: str | None = None,
    state_definitions: dict | None = None,
) -> dict:
    async with get_db() as db:
        ts = now_iso()
        env_json = json.dumps(env_overrides) if env_overrides else None
        state_json = json.dumps(state_definitions) if state_definitions else None
        await db.execute(
            """INSERT INTO projects
               (id, repo, default_branch, working_dir, setup_command, teardown_command,
                test_command, env_overrides, max_turns, max_wall_clock, claude_md_path, model,
                state_definitions, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, repo, default_branch, working_dir, setup_command, teardown_command,
             test_command, env_json, max_turns, max_wall_clock, claude_md_path, model,
             state_json, ts),
        )
        await db.commit()
        return {
            "id": id, "repo": repo, "default_branch": default_branch,
            "working_dir": working_dir, "setup_command": setup_command,
            "teardown_command": teardown_command, "test_command": test_command,
            "env_overrides": env_overrides, "max_turns": max_turns,
            "max_wall_clock": max_wall_clock, "claude_md_path": claude_md_path,
            "model": model, "state_definitions": state_definitions,
            "created_at": ts,
        }


async def get_project(id: str) -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (id,))
        if not rows:
            return None
        p = dict(rows[0])
        if p.get("env_overrides"):
            p["env_overrides"] = json.loads(p["env_overrides"])
        if p.get("state_definitions"):
            p["state_definitions"] = json.loads(p["state_definitions"])
        return p


async def update_project(project_id: str, **fields) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not rows:
            raise ValueError(f"Project '{project_id}' not found")

        if "env_overrides" in fields and isinstance(fields["env_overrides"], dict):
            fields["env_overrides"] = json.dumps(fields["env_overrides"])
        if "state_definitions" in fields and isinstance(fields["state_definitions"], dict):
            fields["state_definitions"] = json.dumps(fields["state_definitions"])

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [project_id]
        await db.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", values)
        await db.commit()

        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (project_id,))
        p = dict(rows[0])
        if p.get("env_overrides"):
            p["env_overrides"] = json.loads(p["env_overrides"])
        if p.get("state_definitions"):
            p["state_definitions"] = json.loads(p["state_definitions"])
        return p


async def list_projects() -> list[dict]:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM projects ORDER BY created_at DESC")
        projects = []
        for r in rows:
            p = dict(r)
            if p.get("env_overrides"):
                p["env_overrides"] = json.loads(p["env_overrides"])
            if p.get("state_definitions"):
                p["state_definitions"] = json.loads(p["state_definitions"])
            projects.append(p)
        return projects


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

async def create_task(
    id: str, project_id: str, goal: str, branch: str | None = None,
    max_turns: int | None = None, max_wall_clock: int | None = None,
    jira_ticket: str | None = None, conversation_id: str | None = None,
    model: str | None = None, auto_test: bool = True,
    depends_on: str | None = None,
    auto_review: bool = True, review_model: str | None = None,
    parent_task_id: str | None = None, auto_pr: bool = False,
    component_id: str | None = None,
    claude_chat_url: str | None = None,
    auto_merge: bool = False,
    auto_release_worktree: bool = True,
    base_branch: str | None = None,
) -> dict:
    async with get_db() as db:
        # Verify project exists
        rows = await db.execute_fetchall("SELECT id FROM projects WHERE id = ?", (project_id,))
        if not rows:
            raise ValueError(f"Project '{project_id}' not found")

        ts = now_iso()
        # Use short name (after project prefix) for branch to avoid slash issues
        short_name = id.split("/")[-1] if "/" in id else id
        branch = branch or short_name
        await db.execute(
            """INSERT INTO tasks
               (id, project_id, goal, status, branch, max_turns, max_wall_clock,
                jira_ticket, conversation_id, model, auto_test, depends_on,
                auto_review, review_model, parent_task_id, auto_pr, component_id,
                claude_chat_url, auto_merge, auto_release_worktree, base_branch,
                created_at, updated_at)
               VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, project_id, goal, branch, max_turns, max_wall_clock,
             jira_ticket, conversation_id, model, auto_test, depends_on,
             auto_review, review_model, parent_task_id, auto_pr, component_id,
             claude_chat_url, auto_merge, auto_release_worktree, base_branch,
             ts, ts),
        )
        await db.commit()
        return {
            "id": id, "project_id": project_id, "goal": goal, "status": "ready",
            "phase": None, "branch": branch, "worktree_path": None,
            "max_turns": max_turns, "max_wall_clock": max_wall_clock,
            "jira_ticket": jira_ticket, "conversation_id": conversation_id,
            "model": model, "auto_test": auto_test, "depends_on": depends_on,
            "auto_review": auto_review, "review_model": review_model,
            "parent_task_id": parent_task_id, "auto_pr": auto_pr,
            "component_id": component_id, "claude_chat_url": claude_chat_url,
            "auto_merge": auto_merge, "auto_release_worktree": auto_release_worktree,
            "base_branch": base_branch,
            "created_at": ts, "updated_at": ts,
        }


async def get_task(id: str) -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (id,))
        if not rows:
            return None
        return dict(rows[0])


TASK_MUTABLE_FIELDS = {
    "status", "phase", "branch", "worktree_path", "session_id", "pid",
    "max_turns", "max_wall_clock",
    "total_input_tokens", "total_output_tokens", "total_cost_usd",
    "dispatch_count", "last_activity", "updated_at",
    "jira_ticket", "conversation_id",
    "auto_test", "gate_status", "gate_retries", "max_gate_retries", "gate_passed_at",
    "depends_on", "auto_review", "review_model", "parent_task_id", "auto_pr",
    "component_id", "model", "claude_chat_url",
    # v5 migration toolkit fields
    "base_branch", "branch_target",
    "max_test_retries", "max_review_retries",
    # v5 auto-merge-queue fields
    "queued_at", "auto_merge", "auto_release_worktree",
    "pushed_at", "pr_status", "pr_error",
    # v5 crash-recovery fields
    "recovery_count", "last_recovery_at", "recovery_priority",
    # v5 realtime-output fields
    "last_test_output", "current_attempt",
    # retry scheduling
    "retry_after",
    # hold/approval
    "held",
}


async def update_task(id: str, **fields) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (id,))
        if not rows:
            raise ValueError(f"Task '{id}' not found")
        task = dict(rows[0])

        # Extract tags — handled separately via task_tags table
        tags = fields.pop("tags", None)

        # Validate component_id if provided
        if "component_id" in fields and fields["component_id"] is not None:
            comp_rows = await db.execute_fetchall(
                "SELECT id FROM components WHERE id = ?", (fields["component_id"],)
            )
            if not comp_rows:
                raise ValueError(f"Component '{fields['component_id']}' not found")

        # Filter to allowed fields to prevent SQL column injection
        col_fields = {k: v for k, v in fields.items() if k in TASK_MUTABLE_FIELDS}
        col_fields["updated_at"] = now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in col_fields)
        values = list(col_fields.values()) + [id]
        await db.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)

        # Update tags if provided
        if tags is not None:
            await db.execute("DELETE FROM task_tags WHERE task_id = ?", (id,))
            for tag in tags:
                await db.execute(
                    "INSERT OR IGNORE INTO task_tags (task_id, tag) VALUES (?, ?)",
                    (id, tag.strip().lower()),
                )

        await db.commit()

        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (id,))
        result = dict(rows[0])
        tag_rows = await db.execute_fetchall(
            "SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (id,)
        )
        result["tags"] = [r["tag"] for r in tag_rows]
        return result


async def bulk_update_tasks(task_ids: list[str], **fields) -> int:
    """Apply the same field updates to multiple tasks. Returns count of updated tasks."""
    count = 0
    for task_id in task_ids:
        try:
            await update_task(task_id, **fields)
            count += 1
        except ValueError:
            pass  # skip tasks that don't exist
    return count


async def move_task(task_id: str, component_id: str) -> dict:
    """Reassign a task to a component. Validates component exists and belongs to same project."""
    async with get_db() as db:
        task_rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not task_rows:
            raise ValueError(f"Task '{task_id}' not found")
        task = dict(task_rows[0])

        comp_rows = await db.execute_fetchall("SELECT * FROM components WHERE id = ?", (component_id,))
        if not comp_rows:
            raise ValueError(f"Component '{component_id}' not found")
        component = dict(comp_rows[0])

        if component["project_id"] != task["project_id"]:
            raise ValueError(
                f"Component '{component_id}' belongs to project '{component['project_id']}', "
                f"but task '{task_id}' belongs to project '{task['project_id']}'"
            )

    return await update_task(task_id, component_id=component_id)


async def list_tasks(project_id: str | None = None, status: str | None = None, tag: str | None = None, component_id: str | None = None, active_only: bool = False) -> list[dict]:
    async with get_db() as db:
        conditions = []
        params: list = []

        if project_id:
            conditions.append("t.project_id = ?")
            params.append(project_id)
        if status:
            conditions.append("t.status = ?")
            params.append(status)
        if component_id:
            conditions.append("t.component_id = ?")
            params.append(component_id)
        if tag:
            conditions.append("EXISTS (SELECT 1 FROM task_tags tt WHERE tt.task_id = t.id AND tt.tag = ?)")
            params.append(tag.strip().lower())
        if active_only:
            # Exclude cancelled tasks and stale error/conflict tasks that have exhausted retries
            conditions.append("t.status != 'cancelled'")
            conditions.append(
                "NOT (t.pr_status IN ('error', 'conflict') AND t.gate_retries >= t.max_gate_retries)"
            )

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        query = f"""
            SELECT t.*,
                (SELECT COUNT(*) FROM task_checklist WHERE task_id = t.id) as checklist_total,
                (SELECT COUNT(*) FROM task_checklist WHERE task_id = t.id AND done = TRUE) as checklist_done,
                (SELECT ref FROM task_artifacts WHERE task_id = t.id AND type = 'pr_url' LIMIT 1) as pr_url
            FROM tasks t
            {where}
            ORDER BY t.updated_at DESC
        """
        rows = await db.execute_fetchall(query, params)
        tasks = []
        for r in rows:
            task = dict(r)
            # Fetch tags for each task
            tag_rows = await db.execute_fetchall(
                "SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (task["id"],),
            )
            task["tags"] = [tr["tag"] for tr in tag_rows]
            tasks.append(task)
        return tasks


async def get_project_task_counts() -> dict[str, dict]:
    """Get task counts and total cost per project in a single query."""
    async with get_db() as db:
        rows = await db.execute_fetchall("""
            SELECT project_id,
                   COUNT(*) as total_tasks,
                   SUM(CASE WHEN status = 'working' THEN 1 ELSE 0 END) as active_task_count,
                   COALESCE(SUM(total_cost_usd), 0) as total_cost
            FROM tasks
            GROUP BY project_id
        """)
        return {
            r["project_id"]: {
                "total_tasks": r["total_tasks"],
                "active_task_count": r["active_task_count"],
                "total_cost": round(r["total_cost"], 2),
            }
            for r in rows
        }


async def get_recent_activity(limit: int = 5) -> list[dict]:
    """Get recent significant task messages across all projects."""
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT m.task_id, m.type AS event_type, m.title, m.created_at
            FROM messages m
            WHERE m.type IN ('result', 'status', 'review', 'question')
            ORDER BY m.created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in rows]


async def get_dependents(task_id: str) -> list[dict]:
    """Get tasks that depend on the given task."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM tasks WHERE depends_on = ? ORDER BY created_at", (task_id,),
        )
        return [dict(r) for r in rows]


async def get_chain(task_id: str) -> list[dict]:
    """Walk the dependency chain from root to tail, given any member."""
    async with get_db() as db:
        # Walk up to find root
        current_id = task_id
        visited = set()
        while True:
            if current_id in visited:
                break
            visited.add(current_id)
            rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (current_id,))
            if not rows:
                break
            task = dict(rows[0])
            if not task.get("depends_on"):
                break
            current_id = task["depends_on"]

        # Walk down from root
        chain = []
        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (current_id,))
        if rows:
            chain.append(dict(rows[0]))
            while True:
                deps = await db.execute_fetchall(
                    "SELECT * FROM tasks WHERE depends_on = ? ORDER BY created_at LIMIT 1",
                    (chain[-1]["id"],),
                )
                if not deps:
                    break
                chain.append(dict(deps[0]))
        return chain


async def count_active_tasks() -> int:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) as cnt FROM tasks WHERE status = 'working'"
        )
        return rows[0]["cnt"]


async def get_working_tasks_for_conversation(conversation_id: str) -> list[str]:
    """Return task IDs that are currently working and linked to the given conversation."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT id FROM tasks WHERE status = 'working' AND conversation_id = ?",
            (conversation_id,),
        )
        return [r["id"] for r in rows]


async def get_queued_tasks() -> list[dict]:
    """Return ready tasks with queued_at set, ordered FIFO (oldest first).

    Excludes tasks whose depends_on parent hasn't gate-passed yet.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT t.* FROM tasks t
               WHERE t.status = 'ready'
                 AND t.queued_at IS NOT NULL
                 AND (
                   t.depends_on IS NULL
                   OR EXISTS (
                     SELECT 1 FROM tasks p
                     WHERE p.id = t.depends_on AND p.gate_passed_at IS NOT NULL
                   )
                 )
               ORDER BY t.recovery_priority DESC, t.queued_at ASC"""
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Task Messages (reuses message model with task_id)
# ---------------------------------------------------------------------------

async def post_task_message(
    task_id: str, author: str, content: str,
    type: str | None = None, title: str | None = None, pinned: bool = False,
) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id, current_attempt FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

        attempt_number = rows[0]["current_attempt"] or 1

        if pinned:
            await db.execute(
                "UPDATE messages SET pinned = FALSE WHERE task_id = ? AND pinned = TRUE",
                (task_id,),
            )

        ts = now_iso()
        cursor = await db.execute(
            """INSERT INTO messages (task_id, author, type, title, content, pinned, created_at, attempt_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, author, type, title, content, pinned, ts, attempt_number),
        )
        msg_id = cursor.lastrowid

        await db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (ts, task_id))
        await db.commit()
        return {
            "id": msg_id, "task_id": task_id, "author": author,
            "type": type, "title": title, "content": content,
            "pinned": pinned, "created_at": ts, "attempt_number": attempt_number,
        }


async def read_task_messages(
    task_id: str, last_n: int | None = None, after: int | None = None,
    type: str | None = None,
) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

    return await _read_messages(
        filter_column="task_id", filter_value=task_id,
        last_n=last_n, after=after, type=type,
    )


async def get_task_pinned(task_id: str) -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE task_id = ? AND pinned = TRUE LIMIT 1",
            (task_id,),
        )
        return _strip_embedding(dict(rows[0])) if rows else None


# ---------------------------------------------------------------------------
# Semantic search / embeddings
# ---------------------------------------------------------------------------

async def set_message_embedding(message_id: int, embedding_blob: bytes) -> None:
    """Store a packed float32 embedding blob on a message row."""
    async with get_db() as db:
        await db.execute(
            "UPDATE messages SET embedding = ? WHERE id = ?",
            (embedding_blob, message_id),
        )
        await db.commit()


async def search_messages_semantic(
    query_vector: list[float],
    conversation_id: str | None = None,
    project_id: str | None = None,
    type_filter: list[str] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Load candidate messages with embeddings and return them with raw similarity scores.

    Filtering by project_id joins through the conversations table.
    Actual relevance scoring (type weights, pinned boost) is applied by the caller.
    """
    from embedding_service import decode_vector, cosine_similarity

    async with get_db() as db:
        conditions = ["m.embedding IS NOT NULL"]
        params: list = []

        if conversation_id:
            conditions.append("m.conversation_id = ?")
            params.append(conversation_id)

        if project_id:
            # Join conversations to filter by project
            conditions.append(
                "(m.conversation_id IN (SELECT id FROM conversations WHERE project = ?) "
                "OR m.task_id IN (SELECT id FROM tasks WHERE project_id = ?))"
            )
            params.extend([project_id, project_id])

        if type_filter:
            placeholders = ",".join("?" * len(type_filter))
            conditions.append(f"m.type IN ({placeholders})")
            params.extend(type_filter)

        where = " AND ".join(conditions)
        query = f"""
            SELECT m.id, m.conversation_id, m.task_id, m.author, m.type, m.title,
                   m.content, m.pinned, m.created_at, m.embedding
            FROM messages m
            WHERE {where}
        """
        rows = await db.execute_fetchall(query, params)

    # Compute cosine similarity in Python — fine for ~5K messages
    results = []
    for row in rows:
        blob = row["embedding"]
        if not blob:
            continue
        try:
            vec = decode_vector(blob)
        except Exception:
            continue
        sim = cosine_similarity(query_vector, vec)
        results.append({
            "message_id": row["id"],
            "conversation_id": row["conversation_id"],
            "task_id": row["task_id"],
            "author": row["author"],
            "type": row["type"],
            "title": row["title"],
            "content": row["content"],
            "pinned": bool(row["pinned"]),
            "created_at": row["created_at"],
            "similarity": sim,
        })

    # Sort by similarity descending before caller applies weights
    results.sort(key=lambda r: r["similarity"], reverse=True)
    return results[:limit * 3]  # Return extra so caller has room to re-rank


async def get_messages_needing_embedding(batch_size: int = 100) -> list[dict]:
    """Return messages that need embedding: no embedding, content >= 50 chars, not test-result."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT id, conversation_id, task_id, type, content
               FROM messages
               WHERE embedding IS NULL
                 AND length(content) >= 50
                 AND (type IS NULL OR type != 'test-result')
               ORDER BY id ASC
               LIMIT ?""",
            (batch_size,),
        )
        return [dict(r) for r in rows]


async def count_messages_needing_embedding() -> int:
    """Count messages that need embedding."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT COUNT(*) as cnt FROM messages
               WHERE embedding IS NULL
                 AND length(content) >= 50
                 AND (type IS NULL OR type != 'test-result')"""
        )
        return rows[0]["cnt"] if rows else 0


# ---------------------------------------------------------------------------
# Task Checklist
# ---------------------------------------------------------------------------

async def create_checklist_items(task_id: str, items: list[str]) -> list[dict]:
    async with get_db() as db:
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


async def get_checklist(task_id: str) -> list[dict]:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM task_checklist WHERE task_id = ? ORDER BY id", (task_id,),
        )
        return [dict(r) for r in rows]


async def update_checklist_item(item_id: int, done: bool) -> dict:
    async with get_db() as db:
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


async def add_checklist_item(task_id: str, item: str) -> dict:
    """Add a new checklist item to a task."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

        ts = now_iso()
        cursor = await db.execute(
            "INSERT INTO task_checklist (task_id, item, done, updated_at) VALUES (?, ?, FALSE, ?)",
            (task_id, item, ts),
        )
        await db.commit()
        return {"id": cursor.lastrowid, "task_id": task_id, "item": item, "done": False, "updated_at": ts}


async def remove_checklist_item(item_id: int) -> dict:
    """Remove a checklist item by ID."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM task_checklist WHERE id = ?", (item_id,))
        if not rows:
            raise ValueError(f"Checklist item {item_id} not found")

        await db.execute("DELETE FROM task_checklist WHERE id = ?", (item_id,))
        await db.commit()
        item = dict(rows[0])
        item["removed"] = True
        return item


async def update_checklist_item_text(item_id: int, text: str) -> dict:
    """Update the text of a checklist item."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM task_checklist WHERE id = ?", (item_id,))
        if not rows:
            raise ValueError(f"Checklist item {item_id} not found")

        ts = now_iso()
        await db.execute(
            "UPDATE task_checklist SET item = ?, updated_at = ? WHERE id = ?",
            (text, ts, item_id),
        )
        await db.commit()
        item = dict(rows[0])
        item["item"] = text
        item["updated_at"] = ts
        return item


# ---------------------------------------------------------------------------
# Task Artifacts
# ---------------------------------------------------------------------------

async def add_artifact(task_id: str, type: str, ref: str) -> dict:
    async with get_db() as db:
        ts = now_iso()
        cursor = await db.execute(
            "INSERT INTO task_artifacts (task_id, type, ref, created_at) VALUES (?, ?, ?, ?)",
            (task_id, type, ref, ts),
        )
        await db.commit()
        return {"id": cursor.lastrowid, "task_id": task_id, "type": type, "ref": ref, "created_at": ts}


async def get_artifacts(task_id: str) -> list[dict]:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY created_at", (task_id,),
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Task Status (rich)
# ---------------------------------------------------------------------------

async def get_task_status(task_id: str) -> dict:
    """Get comprehensive task status including checklist, messages, artifacts, tags."""
    async with get_db() as db:
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
        task["recent_messages"] = [_strip_embedding(dict(r)) for r in reversed(msg_rows)]

        # Artifacts
        art_rows = await db.execute_fetchall(
            "SELECT * FROM task_artifacts WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        )
        task["artifacts"] = [dict(r) for r in art_rows]

        # Tags
        tag_rows = await db.execute_fetchall(
            "SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (task_id,),
        )
        task["tags"] = [r["tag"] for r in tag_rows]

        # Review subtask (from subtasks table, type='review', most recent)
        review_rows = await db.execute_fetchall(
            """SELECT id, status, model, created_at, completed_at
               FROM subtasks WHERE task_id = ? AND type = 'review'
               ORDER BY rowid DESC LIMIT 1""",
            (task_id,),
        )
        if review_rows:
            rs = dict(review_rows[0])
            now_dt = datetime.now(timezone.utc)
            created_dt = datetime.fromisoformat(rs["created_at"].replace("Z", "+00:00"))
            if rs["status"] == "working" or not rs["completed_at"]:
                elapsed_s = int((now_dt - created_dt).total_seconds())
            else:
                completed_dt = datetime.fromisoformat(rs["completed_at"].replace("Z", "+00:00"))
                elapsed_s = int((completed_dt - created_dt).total_seconds())
            task["review_subtask"] = {
                "task_id": rs["id"],
                "status": rs["status"],
                "session_id": None,
                "elapsed": f"{elapsed_s}s",
                "model": rs["model"],
            }
        else:
            task["review_subtask"] = None

        # Parse last_test_output JSON if present
        if task.get("last_test_output"):
            try:
                task["last_test_output"] = json.loads(task["last_test_output"])
            except (json.JSONDecodeError, TypeError):
                pass  # leave as raw string

        return task


async def get_task_attempts(task_id: str) -> list[dict]:
    """Return messages grouped by attempt_number, each group with an outcome summary.

    Outcome values: "in-progress", "success", "test-failure", "review-rejection",
                    "wall-clock-timeout", "turns-exhausted", "error", "retried"
    """
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

        msg_rows = await db.execute_fetchall(
            """SELECT id, author, type, title, content, created_at, attempt_number
               FROM messages WHERE task_id = ? ORDER BY created_at ASC""",
            (task_id,),
        )
        messages = [dict(r) for r in msg_rows]

    # Group by attempt_number (default to 1 if NULL)
    groups: dict[int, list[dict]] = {}
    for msg in messages:
        attempt = msg.get("attempt_number") or 1
        groups.setdefault(attempt, []).append(msg)

    # Determine outcome for each attempt
    max_attempt = max(groups.keys()) if groups else 1
    result = []
    for attempt_num in sorted(groups.keys()):
        group_messages = groups[attempt_num]
        is_last = attempt_num == max_attempt
        outcome = _determine_attempt_outcome(group_messages, is_last, attempt_num < max_attempt)
        result.append({
            "attempt_number": attempt_num,
            "messages": group_messages,
            "outcome": outcome,
        })

    return result


def _determine_attempt_outcome(messages: list[dict], is_last: bool, has_next: bool) -> str:
    """Determine how an attempt ended based on its messages."""
    # Walk messages in reverse to find the most significant terminal event
    for msg in reversed(messages):
        msg_type = msg.get("type") or ""
        title = (msg.get("title") or "").upper()
        author = msg.get("author") or ""

        if author == "dispatcher":
            if msg_type == "test-result":
                if "FAILED" in title or "FAIL" in title:
                    if has_next:
                        return "test-failure"
                    return "test-failure"
                elif "PASSED" in title or "PASS" in title:
                    if not is_last:
                        return "test-failure"  # more attempts followed
            if "WALL CLOCK" in title or "TIMEOUT" in title:
                return "wall-clock-timeout"
            if "TURNS EXHAUSTED" in title or "TURNS" in title:
                return "turns-exhausted"
            if msg_type == "status" and ("ERROR" in title or "FAILED" in title or "DISPATCH ERROR" in title):
                return "error"

        if msg_type == "review":
            if "APPROVED" in title:
                if not has_next:
                    return "success"
            elif "CHANGES REQUESTED" in title or "REJECT" in title:
                if has_next:
                    return "review-rejection"
                return "review-rejection"

    if has_next:
        return "retried"
    return "in-progress"


def get_merged_state_definitions(project: dict | None = None) -> dict:
    """Merge core state definitions with project-level custom states."""
    merged = dict(CORE_STATE_DEFINITIONS)
    if project and project.get("state_definitions"):
        custom = project["state_definitions"]
        if isinstance(custom, str):
            custom = json.loads(custom)
        merged.update(custom)
    return merged


def get_state_definition(state: str, project: dict | None = None) -> dict:
    """Get the definition for a single state, with fallback."""
    merged = get_merged_state_definitions(project)
    if state in merged:
        return merged[state]
    # Unknown state — return a sensible default
    return {"color": "#6b7280", "label": state.replace("-", " ").title(), "pulse": False}


# ---------------------------------------------------------------------------
# Task Tags
# ---------------------------------------------------------------------------

async def set_task_tags(task_id: str, tags: list[str]) -> list[str]:
    """Set tags for a task (replaces existing tags)."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            raise ValueError(f"Task '{task_id}' not found")

        await db.execute("DELETE FROM task_tags WHERE task_id = ?", (task_id,))
        for tag in tags:
            await db.execute(
                "INSERT OR IGNORE INTO task_tags (task_id, tag) VALUES (?, ?)",
                (task_id, tag.strip().lower()),
            )
        await db.commit()
        return [t.strip().lower() for t in tags]


async def get_task_tags(task_id: str) -> list[str]:
    """Get tags for a task."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (task_id,),
        )
        return [r["tag"] for r in rows]


# ---------------------------------------------------------------------------
# Subtasks
# ---------------------------------------------------------------------------

async def create_subtask(id: str, task_id: str, type: str, prompt: str, model: str = "opus") -> dict:
    """Create a subtask record."""
    async with get_db() as conn:
        ts = now_iso()
        await conn.execute(
            """INSERT INTO subtasks (id, task_id, type, status, model, prompt, created_at)
               VALUES (?, ?, ?, 'working', ?, ?, ?)""",
            (id, task_id, type, model, prompt, ts),
        )
        await conn.commit()
        return {"id": id, "task_id": task_id, "type": type, "status": "working",
                "model": model, "prompt": prompt, "created_at": ts}


async def update_subtask(id: str, **fields) -> dict:
    """Update a subtask. Only allowed fields are updated."""
    async with get_db() as conn:
        allowed = {"status", "result", "input_tokens", "output_tokens",
                    "cost_usd", "duration_ms", "completed_at"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            rows = await conn.execute_fetchall("SELECT * FROM subtasks WHERE id = ?", (id,))
            return dict(rows[0]) if rows else {}
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [id]
        await conn.execute(f"UPDATE subtasks SET {set_clause} WHERE id = ?", values)
        await conn.commit()
        rows = await conn.execute_fetchall("SELECT * FROM subtasks WHERE id = ?", (id,))
        return dict(rows[0]) if rows else {}


async def get_subtasks(task_id: str) -> list[dict]:
    """Get all subtasks for a task, ordered by creation time."""
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT * FROM subtasks WHERE task_id = ? ORDER BY created_at", (task_id,),
        )
        return [dict(r) for r in rows]


async def get_subtask(id: str) -> dict | None:
    """Get a single subtask by ID."""
    async with get_db() as conn:
        rows = await conn.execute_fetchall("SELECT * FROM subtasks WHERE id = ?", (id,))
        return dict(rows[0]) if rows else None


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

COMPONENT_CONFIG_FIELDS = {
    "base_branch", "setup_command", "test_command", "model",
    "auto_test", "auto_review", "review_model",
    "max_test_retries", "max_review_retries",
    "auto_pr", "auto_merge", "max_turns", "max_wall_clock",
}

COMPONENT_MUTABLE_FIELDS = COMPONENT_CONFIG_FIELDS | {
    "name", "description", "phase", "env_overrides", "secrets",
}

SYSTEM_DEFAULTS = {
    "auto_test": True,
    "auto_review": True,
    "review_model": "opus",
    "max_test_retries": 3,
    "max_review_retries": 2,
    "auto_pr": False,
    "auto_merge": False,
    "auto_release_worktree": True,
}


async def create_component(
    id: str, project_id: str, name: str,
    description: str | None = None, phase: str = "planning",
    **config_fields,
) -> dict:
    async with get_db() as db:
        # Verify project exists
        rows = await db.execute_fetchall("SELECT id FROM projects WHERE id = ?", (project_id,))
        if not rows:
            raise ValueError(f"Project '{project_id}' not found")

        ts = now_iso()
        env_json = json.dumps(config_fields.pop("env_overrides")) if "env_overrides" in config_fields and config_fields["env_overrides"] is not None else config_fields.pop("env_overrides", None)
        secrets_json = json.dumps(config_fields.pop("secrets")) if "secrets" in config_fields and config_fields["secrets"] is not None else config_fields.pop("secrets", None)

        # Filter to valid config fields
        valid_config = {k: v for k, v in config_fields.items() if k in COMPONENT_CONFIG_FIELDS}

        cols = ["id", "project_id", "name", "description", "phase", "env_overrides", "secrets", "created_at", "updated_at"]
        vals = [id, project_id, name, description, phase, env_json, secrets_json, ts, ts]

        for k, v in valid_config.items():
            cols.append(k)
            vals.append(v)

        placeholders = ", ".join("?" for _ in vals)
        col_str = ", ".join(cols)
        await db.execute(f"INSERT INTO components ({col_str}) VALUES ({placeholders})", vals)
        await db.commit()

        result = {
            "id": id, "project_id": project_id, "name": name,
            "description": description, "phase": phase,
            "env_overrides": json.loads(env_json) if env_json else None,
            "secrets": json.loads(secrets_json) if secrets_json else None,
            "created_at": ts, "updated_at": ts,
        }
        result.update(valid_config)
        return result


async def get_component(id: str) -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM components WHERE id = ?", (id,))
        if not rows:
            return None
        c = dict(rows[0])
        if c.get("env_overrides"):
            c["env_overrides"] = json.loads(c["env_overrides"])
        if c.get("secrets"):
            c["secrets"] = json.loads(c["secrets"])

        # Task summary + full task list
        task_rows = await db.execute_fetchall(
            """SELECT status, COUNT(*) as cnt, COALESCE(SUM(total_cost_usd), 0) as cost
               FROM tasks WHERE component_id = ? GROUP BY status""",
            (id,),
        )
        task_summary = {}
        total_tasks = 0
        total_cost = 0.0
        active_tasks = 0
        done_tasks = 0
        failed_tasks = 0
        for r in task_rows:
            task_summary[r["status"]] = r["cnt"]
            total_tasks += r["cnt"]
            total_cost += r["cost"]
            if r["status"] == "working":
                active_tasks = r["cnt"]
            if r["status"] in ("completed", "merged"):
                done_tasks += r["cnt"]
            if r["status"] == "failed":
                failed_tasks += r["cnt"]
        c["task_summary"] = {
            "by_status": task_summary,
            "total": total_tasks,
            "active": active_tasks,
            "total_cost": round(total_cost, 2),
        }
        # Flat fields for frontend progress bar
        c["total_tasks"] = total_tasks
        c["done_tasks"] = done_tasks
        c["active_tasks"] = active_tasks
        c["failed_tasks"] = failed_tasks
        c["total_cost"] = round(total_cost, 2)

        # Full task list for component detail view
        all_task_rows = await db.execute_fetchall(
            """SELECT t.*,
                (SELECT COUNT(*) FROM task_checklist WHERE task_id = t.id) as checklist_total,
                (SELECT COUNT(*) FROM task_checklist WHERE task_id = t.id AND done = TRUE) as checklist_done,
                (SELECT ref FROM task_artifacts WHERE task_id = t.id AND type = 'pr_url' LIMIT 1) as pr_url
               FROM tasks t WHERE t.component_id = ? ORDER BY t.created_at DESC""",
            (id,),
        )
        all_tasks = []
        for t in all_task_rows:
            task = dict(t)
            tag_rows = await db.execute_fetchall(
                "SELECT tag FROM task_tags WHERE task_id = ? ORDER BY tag", (task["id"],)
            )
            task["tags"] = [tr["tag"] for tr in tag_rows]
            all_tasks.append(task)
        # Find external dependencies — tasks in other components that our tasks depend on
        component_task_ids = {t["id"] for t in all_tasks}
        external_dep_ids = set()
        for t in all_tasks:
            dep = t.get("depends_on")
            if dep and dep not in component_task_ids:
                external_dep_ids.add(dep)

        for ext_id in external_dep_ids:
            ext_rows = await db.execute_fetchall(
                "SELECT t.*, c.name as component_name FROM tasks t LEFT JOIN components c ON t.component_id = c.id WHERE t.id = ?",
                (ext_id,),
            )
            if ext_rows:
                ext_task = dict(ext_rows[0])
                ext_task["_ghost"] = True  # marker for frontend
                ext_task["tags"] = []
                all_tasks.append(ext_task)

        c["tasks"] = all_tasks

        # Linked conversations (return objects with id + goal)
        conv_rows = await db.execute_fetchall(
            """SELECT cc.conversation_id AS id, conv.goal
               FROM component_conversations cc
               LEFT JOIN conversations conv ON conv.id = cc.conversation_id
               WHERE cc.component_id = ?""",
            (id,),
        )
        c["conversations"] = [dict(r) for r in conv_rows]

        return c


async def update_component(id: str, **fields) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM components WHERE id = ?", (id,))
        if not rows:
            raise ValueError(f"Component '{id}' not found")

        # Handle JSON fields
        if "env_overrides" in fields:
            val = fields["env_overrides"]
            fields["env_overrides"] = json.dumps(val) if isinstance(val, dict) else val
        if "secrets" in fields:
            val = fields["secrets"]
            fields["secrets"] = json.dumps(val) if isinstance(val, dict) else val

        # Filter to allowed fields
        fields = {k: v for k, v in fields.items() if k in COMPONENT_MUTABLE_FIELDS}
        fields["updated_at"] = now_iso()

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [id]
        await db.execute(f"UPDATE components SET {set_clause} WHERE id = ?", values)
        await db.commit()

        rows = await db.execute_fetchall("SELECT * FROM components WHERE id = ?", (id,))
        c = dict(rows[0])
        if c.get("env_overrides"):
            c["env_overrides"] = json.loads(c["env_overrides"])
        if c.get("secrets"):
            c["secrets"] = json.loads(c["secrets"])
        return c


async def list_components(project_id: str | None = None) -> list[dict]:
    async with get_db() as db:
        base_query = """SELECT c.*,
                    (SELECT COUNT(*) FROM tasks WHERE component_id = c.id) as total_tasks,
                    (SELECT COUNT(*) FROM tasks WHERE component_id = c.id AND status = 'working') as active_tasks,
                    (SELECT COUNT(*) FROM tasks WHERE component_id = c.id AND status IN ('completed', 'merged')) as done_tasks,
                    (SELECT COUNT(*) FROM tasks WHERE component_id = c.id AND status IN ('failed', 'needs-review')) as failed_tasks,
                    (SELECT COALESCE(SUM(total_cost_usd), 0) FROM tasks WHERE component_id = c.id) as total_cost,
                    (SELECT COUNT(*) FROM component_conversations WHERE component_id = c.id) as conversation_count,
                    (SELECT COUNT(*) FROM punchlist WHERE component_id = c.id AND status != 'done') as open_punchlist
                   FROM components c"""
        if project_id:
            rows = await db.execute_fetchall(
                base_query + " WHERE c.project_id = ? ORDER BY c.created_at DESC",
                (project_id,),
            )
        else:
            rows = await db.execute_fetchall(
                base_query + " ORDER BY c.created_at DESC"
            )
        results = []
        for r in rows:
            c = dict(r)
            c["total_cost"] = round(c.get("total_cost", 0), 2)
            if c.get("env_overrides"):
                c["env_overrides"] = json.loads(c["env_overrides"])
            if c.get("secrets"):
                c["secrets"] = json.loads(c["secrets"])
            results.append(c)
        return results


# ---------------------------------------------------------------------------
# Component Conversations
# ---------------------------------------------------------------------------

async def link_conversation(component_id: str, conversation_id: str) -> dict:
    async with get_db() as db:
        # Verify component exists
        rows = await db.execute_fetchall("SELECT id FROM components WHERE id = ?", (component_id,))
        if not rows:
            raise ValueError(f"Component '{component_id}' not found")
        await db.execute(
            "INSERT OR IGNORE INTO component_conversations (component_id, conversation_id) VALUES (?, ?)",
            (component_id, conversation_id),
        )
        await db.commit()
        return {"component_id": component_id, "conversation_id": conversation_id, "linked": True}


async def unlink_conversation(component_id: str, conversation_id: str) -> dict:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM component_conversations WHERE component_id = ? AND conversation_id = ?",
            (component_id, conversation_id),
        )
        await db.commit()
        return {"component_id": component_id, "conversation_id": conversation_id, "unlinked": True}


async def get_component_conversations(component_id: str) -> list[str]:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT conversation_id FROM component_conversations WHERE component_id = ?",
            (component_id,),
        )
        return [r["conversation_id"] for r in rows]


# ---------------------------------------------------------------------------
# Config Inheritance
# ---------------------------------------------------------------------------

async def resolve_config(task_id: str) -> dict:
    """Resolve effective config for a task: task → component → project → system defaults.

    For scalar fields, returns the most-specific non-null value.
    For env_overrides and secrets, performs shallow merge (most-specific key wins).
    """
    async with get_db() as db:
        task_rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not task_rows:
            raise ValueError(f"Task '{task_id}' not found")
        task = dict(task_rows[0])

        project_rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (task["project_id"],))
        project = dict(project_rows[0]) if project_rows else {}

        component = None
        if task.get("component_id"):
            comp_rows = await db.execute_fetchall("SELECT * FROM components WHERE id = ?", (task["component_id"],))
            if comp_rows:
                component = dict(comp_rows[0])

    resolved = {}

    # Boolean fields that need normalization from SQLite 0/1
    bool_fields = {"auto_test", "auto_review", "auto_pr", "auto_merge", "auto_release_worktree"}

    # Scalar config fields: task > component > project > system default
    scalar_fields = [
        "base_branch", "model", "auto_test", "auto_review", "review_model",
        "max_test_retries", "max_review_retries", "auto_pr", "auto_merge",
        "auto_release_worktree",
        "setup_command", "test_command", "max_turns", "max_wall_clock",
    ]

    for field in scalar_fields:
        val = task.get(field)
        if val is None and component:
            val = component.get(field)
        if val is None:
            val = project.get(field)
        if val is None:
            val = SYSTEM_DEFAULTS.get(field)
        # Normalize SQLite 0/1 to Python bool
        if field in bool_fields and val is not None:
            val = bool(val)
        resolved[field] = val

    # Shallow merge fields: project (base) ← component ← task (wins)
    for merge_field in ("env_overrides", "secrets"):
        merged = {}
        # Start with project
        pval = project.get(merge_field)
        if isinstance(pval, str):
            pval = json.loads(pval)
        if pval:
            merged.update(pval)
        # Layer component
        if component:
            cval = component.get(merge_field)
            if isinstance(cval, str):
                cval = json.loads(cval)
            if cval:
                merged.update(cval)
        # Layer task (tasks don't currently have env_overrides/secrets columns, but future-proof)
        tval = task.get(merge_field)
        if isinstance(tval, str):
            tval = json.loads(tval)
        if tval:
            merged.update(tval)
        resolved[merge_field] = merged if merged else None

    return resolved


def _make_snippet(content: str, query: str) -> str:
    """Extract a ~120-char snippet around the first match of query in content."""
    lower_content = content.lower()
    idx = lower_content.find(query.lower())
    if idx >= 0:
        start = max(0, idx - 50)
        end = min(len(content), idx + len(query) + 50)
        return ("..." if start > 0 else "") + content[start:end] + ("..." if end < len(content) else "")
    return content[:120] + ("..." if len(content) > 120 else "")


# ---------------------------------------------------------------------------
# Search Task Messages
# ---------------------------------------------------------------------------

async def get_activity(
    project_id: str | None = None, limit: int = 30, offset: int = 0
) -> list[dict]:
    """Get recent significant task messages for the activity feed."""
    async with get_db() as db:
        conditions = [
            "m.task_id IS NOT NULL",
            "m.type IN ('result', 'test-result', 'review', 'handoff', 'status')",
        ]
        params: list = []

        if project_id:
            conditions.append("t.project_id = ?")
            params.append(project_id)

        where = " AND ".join(conditions)
        params.extend([limit, offset])

        rows = await db.execute_fetchall(
            f"""
            SELECT
                m.id, m.task_id, m.type AS event_type,
                m.content, m.title, m.created_at,
                t.goal AS task_goal, t.project_id,
                t.total_cost_usd, t.status AS task_status
            FROM messages m
            JOIN tasks t ON m.task_id = t.id
            WHERE {where}
            ORDER BY m.created_at DESC
            LIMIT ? OFFSET ?
            """,
            params,
        )
        return [dict(r) for r in rows]


async def get_component_activity(
    component_id: str, limit: int = 50
) -> list[dict]:
    """Get recent significant task messages for tasks belonging to a component."""
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            """
            SELECT
                m.id, m.task_id, m.type, m.type AS event_type,
                m.content, m.title, m.created_at,
                t.goal AS task_goal, t.status AS task_status,
                t.total_cost_usd
            FROM messages m
            JOIN tasks t ON m.task_id = t.id
            WHERE t.component_id = ?
              AND m.type IN ('result', 'status', 'test-result', 'review', 'handoff', 'question')
            ORDER BY m.created_at DESC
            LIMIT ?
            """,
            (component_id, limit),
        )
        results = []
        for r in rows:
            ev = dict(r)
            # Add a brief summary for the timeline
            content = ev.get("content") or ""
            first_line = next((l.strip() for l in content.split("\n") if l.strip()), "")
            clean = first_line.lstrip("#").strip().replace("**", "")
            ev["summary"] = clean[:120] + "…" if len(clean) > 120 else clean
            results.append(ev)
        return results


async def search_task_messages(query: str, project_id: str | None = None, limit: int = 20) -> list[dict]:
    """Search across all task message content using LIKE."""
    async with get_db() as db:
        conditions = ["m.task_id IS NOT NULL", "m.content LIKE ?"]
        params: list = [f"%{query}%"]

        if project_id:
            conditions.append("t.project_id = ?")
            params.append(project_id)

        where = " AND ".join(conditions)
        sql = f"""
            SELECT m.id, m.task_id, m.author, m.type, m.content, m.created_at,
                   t.project_id
            FROM messages m
            JOIN tasks t ON t.id = m.task_id
            WHERE {where}
            ORDER BY m.created_at DESC
            LIMIT ?
        """
        params.append(limit)
        rows = await db.execute_fetchall(sql, params)

        results = []
        for r in rows:
            row = dict(r)
            content = row["content"] or ""
            row["snippet"] = _make_snippet(content, query)
            del row["content"]
            results.append(row)

        return results


# ---------------------------------------------------------------------------
# search_component — unified search across conversations + tasks + Graphiti
# ---------------------------------------------------------------------------

async def search_component(
    component_id: str,
    query: str,
    include_graphiti: bool = False,
    limit: int = 20,
) -> dict:
    """Search across all content linked to a component.

    Searches:
    1. Messages in conversations linked to this component
    2. Messages in tasks belonging to this component
    3. Optionally, Graphiti via the project's connectors config

    Returns {results: [...], sources: [...], graphiti_error: str|None}
    Each result: {source, id, author, type, created_at, snippet, [conversation_id|task_id]}
    """
    async with get_db() as db:
        # Verify component exists and get project_id
        comp_rows = await db.execute_fetchall("SELECT id, project_id FROM components WHERE id = ?", (component_id,))
        if not comp_rows:
            raise ValueError(f"Component '{component_id}' not found")
        project_id = comp_rows[0]["project_id"]

        # --- Search conversation messages ---
        conv_rows = await db.execute_fetchall(
            "SELECT conversation_id FROM component_conversations WHERE component_id = ?",
            (component_id,),
        )
        conv_ids = [r["conversation_id"] for r in conv_rows]

        conversation_results = []
        if conv_ids:
            placeholders = ",".join("?" * len(conv_ids))
            conv_sql = f"""
                SELECT m.id, m.conversation_id, m.author, m.type, m.content, m.created_at
                FROM messages m
                WHERE m.conversation_id IN ({placeholders}) AND m.content LIKE ?
                ORDER BY m.created_at DESC
                LIMIT ?
            """
            conv_msg_rows = await db.execute_fetchall(
                conv_sql, conv_ids + [f"%{query}%", limit]
            )
            for r in conv_msg_rows:
                row = dict(r)
                content = row.pop("content", "") or ""
                row["snippet"] = _make_snippet(content, query)
                row["source"] = "conversation"
                conversation_results.append(row)

        # --- Search task messages ---
        task_rows = await db.execute_fetchall(
            "SELECT id FROM tasks WHERE component_id = ?",
            (component_id,),
        )
        task_ids = [r["id"] for r in task_rows]

        task_results = []
        if task_ids:
            placeholders = ",".join("?" * len(task_ids))
            task_sql = f"""
                SELECT m.id, m.task_id, m.author, m.type, m.content, m.created_at
                FROM messages m
                WHERE m.task_id IN ({placeholders}) AND m.content LIKE ?
                ORDER BY m.created_at DESC
                LIMIT ?
            """
            task_msg_rows = await db.execute_fetchall(
                task_sql, task_ids + [f"%{query}%", limit]
            )
            for r in task_msg_rows:
                row = dict(r)
                content = row.pop("content", "") or ""
                row["snippet"] = _make_snippet(content, query)
                row["source"] = "task"
                task_results.append(row)

        # Merge and sort by created_at descending
        all_results = conversation_results + task_results
        all_results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        all_results = all_results[:limit]

        sources = list({r["source"] for r in all_results})

        # --- Graphiti proxy (optional) ---
        graphiti_results = []
        graphiti_error = None

        if include_graphiti:
            proj_rows = await db.execute_fetchall(
                "SELECT connectors FROM projects WHERE id = ?", (project_id,)
            )
            connectors_raw = proj_rows[0]["connectors"] if proj_rows else None
            connectors = json.loads(connectors_raw) if connectors_raw else {}
            graphiti_cfg = connectors.get("graphiti", {})
            graphiti_url = graphiti_cfg.get("url")
            graphiti_group_id = graphiti_cfg.get("group_id")

            if graphiti_url and graphiti_group_id:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.post(
                            f"{graphiti_url.rstrip('/')}/search",
                            json={"query": query, "group_id": graphiti_group_id},
                        )
                        resp.raise_for_status()
                        data = resp.json()
                        raw_results = data if isinstance(data, list) else data.get("results", [])
                        for item in raw_results:
                            graphiti_results.append({
                                "source": "graphiti",
                                "id": item.get("uuid") or item.get("id"),
                                "author": item.get("source_description") or "graphiti",
                                "type": item.get("type"),
                                "created_at": item.get("created_at"),
                                "snippet": item.get("fact") or item.get("content") or item.get("name", ""),
                            })
                        if "graphiti" not in sources and graphiti_results:
                            sources.append("graphiti")
                except Exception as e:
                    graphiti_error = str(e)

    return {
        "results": all_results + graphiti_results,
        "sources": sources,
        "total": len(all_results) + len(graphiti_results),
        "graphiti_error": graphiti_error,
    }


# ---------------------------------------------------------------------------
# Punchlist
# ---------------------------------------------------------------------------

async def add_punchlist_item(component_id: str, item: str, author: str | None = None) -> dict:
    """Add a punchlist item for a component. Raises ValueError if component not found."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT id FROM components WHERE id = ?", (component_id,))
        if not rows:
            raise ValueError(f"Component '{component_id}' not found")
        ts = now_iso()
        cursor = await db.execute(
            """INSERT INTO punchlist (component_id, item, status, author, created_at)
               VALUES (?, ?, 'open', ?, ?)""",
            (component_id, item, author, ts),
        )
        await db.commit()
        return {
            "id": cursor.lastrowid,
            "component_id": component_id,
            "item": item,
            "status": "open",
            "claimed_by": None,
            "resolved_by": None,
            "resolved_at": None,
            "author": author,
            "created_at": ts,
        }


async def get_punchlist_item(item_id: int) -> dict | None:
    """Get a single punchlist item by ID."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM punchlist WHERE id = ?", (item_id,))
        if not rows:
            return None
        return dict(rows[0])


async def list_punchlist(
    component_id: str,
    include_done: bool = False,
    claimed_by: str | None = None,
) -> list[dict]:
    """List punchlist items for a component. Excludes 'done' by default."""
    async with get_db() as db:
        conditions = ["component_id = ?"]
        params: list = [component_id]
        if not include_done:
            conditions.append("status != 'done'")
        if claimed_by is not None:
            conditions.append("claimed_by = ?")
            params.append(claimed_by)
        where = " AND ".join(conditions)
        rows = await db.execute_fetchall(
            f"SELECT * FROM punchlist WHERE {where} ORDER BY id ASC", params
        )
        return [dict(r) for r in rows]


async def claim_punchlist_item(item_id: int, task_id: str) -> dict:
    """Claim a punchlist item for a task. Raises ValueError if not found or already done."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM punchlist WHERE id = ?", (item_id,))
        if not rows:
            raise ValueError(f"Punchlist item {item_id} not found")
        item = dict(rows[0])
        if item["status"] == "done":
            raise ValueError(f"Punchlist item {item_id} is already done")
        await db.execute(
            "UPDATE punchlist SET status = 'claimed', claimed_by = ? WHERE id = ?",
            (task_id, item_id),
        )
        await db.commit()
        item["status"] = "claimed"
        item["claimed_by"] = task_id
        return item


async def resolve_punchlist_items_for_task(task_id: str) -> int:
    """Mark all 'claimed' items for this task as 'done'. Returns count resolved."""
    async with get_db() as db:
        ts = now_iso()
        cursor = await db.execute(
            """UPDATE punchlist SET status = 'done', resolved_by = ?, resolved_at = ?
               WHERE claimed_by = ? AND status = 'claimed'""",
            (task_id, ts, task_id),
        )
        await db.commit()
        return cursor.rowcount


# Aliases used by dashboard_api.py
async def create_punchlist_item(component_id: str, item: str) -> dict:
    """Alias for add_punchlist_item (used by dashboard API)."""
    return await add_punchlist_item(component_id, item)


async def update_punchlist_item(item_id: int, **fields) -> dict:
    """Update arbitrary fields on a punchlist item."""
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM punchlist WHERE id = ?", (item_id,))
        if not rows:
            raise ValueError(f"Punchlist item {item_id} not found")
        allowed = {"status", "claimed_by", "resolved_by", "resolved_at", "item"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            await db.execute(
                f"UPDATE punchlist SET {set_clause} WHERE id = ?",
                (*updates.values(), item_id),
            )
            await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM punchlist WHERE id = ?", (item_id,))
        return dict(rows[0])


async def delete_punchlist_item(item_id: int) -> bool:
    """Delete a punchlist item by ID."""
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM punchlist WHERE id = ?", (item_id,))
        await db.commit()
        return cursor.rowcount > 0


async def revert_punchlist_items_for_task(task_id: str) -> int:
    """Revert 'claimed' items for this task back to 'open'. Returns count reverted."""
    async with get_db() as db:
        cursor = await db.execute(
            """UPDATE punchlist SET status = 'open', claimed_by = NULL
               WHERE claimed_by = ? AND status = 'claimed'""",
            (task_id,),
        )
        await db.commit()
        return cursor.rowcount
