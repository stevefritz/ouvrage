import aiosqlite
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("SWITCHBOARD_DB", "./data/switchboard.db")

# Global defaults for task resource limits
DEFAULT_MAX_TURNS = 200
DEFAULT_MAX_WALL_CLOCK = 60  # minutes
DEFAULT_MAX_CONCURRENT = 3

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
        # Separate gate counters (gate-separate-counters feature)
        if "test_retries" not in task_col_names:
            # Migrate existing gate_retries → test_retries (worst-case assumption)
            await conn.execute("ALTER TABLE tasks ADD COLUMN test_retries INTEGER DEFAULT 0")
            await conn.execute("UPDATE tasks SET test_retries = gate_retries WHERE gate_retries > 0")
        if "review_retries" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN review_retries INTEGER DEFAULT 0")
        if "max_test_retries" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN max_test_retries INTEGER DEFAULT 3")
        if "max_review_retries" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN max_review_retries INTEGER DEFAULT 3")
        if "max_total_gate_retries" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN max_total_gate_retries INTEGER DEFAULT 6")

        # Migrate projects table: add model and gate retry limit columns if missing
        project_columns = await conn.execute_fetchall("PRAGMA table_info(projects)")
        project_col_names = [c["name"] for c in project_columns]
        if "model" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN model TEXT")
        if "max_test_retries" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN max_test_retries INTEGER")
        if "max_review_retries" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN max_review_retries INTEGER")
        if "max_total_gate_retries" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN max_total_gate_retries INTEGER")

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
        """)

        await conn.commit()


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Shared helpers — deduplicated read logic
# ---------------------------------------------------------------------------

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
        pinned = [dict(r) for r in pinned_rows]
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
        messages = [dict(r) for r in rows if r["id"] not in pinned_ids]

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

async def create_conversation(id: str, project: str, goal: str) -> dict:
    async with get_db() as db:
        ts = now_iso()
        await db.execute(
            "INSERT INTO conversations (id, project, goal, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (id, project, goal, ts, ts),
        )
        await db.commit()
        return {"id": id, "project": project, "goal": goal, "archived": False, "created_at": ts, "updated_at": ts}


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
        return dict(rows[0]) if rows else None


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
    max_test_retries: int | None = None, max_review_retries: int | None = None,
    max_total_gate_retries: int | None = None,
) -> dict:
    async with get_db() as db:
        ts = now_iso()
        env_json = json.dumps(env_overrides) if env_overrides else None
        await db.execute(
            """INSERT INTO projects
               (id, repo, default_branch, working_dir, setup_command, teardown_command,
                test_command, env_overrides, max_turns, max_wall_clock, claude_md_path, model,
                max_test_retries, max_review_retries, max_total_gate_retries, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, repo, default_branch, working_dir, setup_command, teardown_command,
             test_command, env_json, max_turns, max_wall_clock, claude_md_path, model,
             max_test_retries, max_review_retries, max_total_gate_retries, ts),
        )
        await db.commit()
        return {
            "id": id, "repo": repo, "default_branch": default_branch,
            "working_dir": working_dir, "setup_command": setup_command,
            "teardown_command": teardown_command, "test_command": test_command,
            "env_overrides": env_overrides, "max_turns": max_turns,
            "max_wall_clock": max_wall_clock, "claude_md_path": claude_md_path,
            "model": model, "max_test_retries": max_test_retries,
            "max_review_retries": max_review_retries,
            "max_total_gate_retries": max_total_gate_retries,
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
        return p


async def update_project(project_id: str, **fields) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (project_id,))
        if not rows:
            raise ValueError(f"Project '{project_id}' not found")

        if "env_overrides" in fields and isinstance(fields["env_overrides"], dict):
            fields["env_overrides"] = json.dumps(fields["env_overrides"])

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [project_id]
        await db.execute(f"UPDATE projects SET {set_clause} WHERE id = ?", values)
        await db.commit()

        rows = await db.execute_fetchall("SELECT * FROM projects WHERE id = ?", (project_id,))
        p = dict(rows[0])
        if p.get("env_overrides"):
            p["env_overrides"] = json.loads(p["env_overrides"])
        return p


async def list_projects() -> list[dict]:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM projects ORDER BY created_at DESC")
        projects = []
        for r in rows:
            p = dict(r)
            if p.get("env_overrides"):
                p["env_overrides"] = json.loads(p["env_overrides"])
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
    max_test_retries: int = 3, max_review_retries: int = 3,
    max_total_gate_retries: int = 6,
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
                auto_review, review_model, parent_task_id, auto_pr,
                max_test_retries, max_review_retries, max_total_gate_retries,
                created_at, updated_at)
               VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (id, project_id, goal, branch, max_turns, max_wall_clock,
             jira_ticket, conversation_id, model, auto_test, depends_on,
             auto_review, review_model, parent_task_id, auto_pr,
             max_test_retries, max_review_retries, max_total_gate_retries,
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
            "test_retries": 0, "review_retries": 0,
            "max_test_retries": max_test_retries, "max_review_retries": max_review_retries,
            "max_total_gate_retries": max_total_gate_retries,
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
    # Separate gate counters
    "test_retries", "review_retries",
    "max_test_retries", "max_review_retries", "max_total_gate_retries",
}


async def update_task(id: str, **fields) -> dict:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (id,))
        if not rows:
            raise ValueError(f"Task '{id}' not found")

        # Filter to allowed fields to prevent SQL column injection
        fields = {k: v for k, v in fields.items() if k in TASK_MUTABLE_FIELDS}
        fields["updated_at"] = now_iso()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [id]
        await db.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
        await db.commit()

        rows = await db.execute_fetchall("SELECT * FROM tasks WHERE id = ?", (id,))
        return dict(rows[0])


async def list_tasks(project_id: str | None = None, status: str | None = None, tag: str | None = None) -> list[dict]:
    async with get_db() as db:
        conditions = []
        params: list = []

        if project_id:
            conditions.append("t.project_id = ?")
            params.append(project_id)
        if status:
            conditions.append("t.status = ?")
            params.append(status)
        if tag:
            conditions.append("EXISTS (SELECT 1 FROM task_tags tt WHERE tt.task_id = t.id AND tt.tag = ?)")
            params.append(tag.strip().lower())

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


# ---------------------------------------------------------------------------
# Task Messages (reuses message model with task_id)
# ---------------------------------------------------------------------------

async def post_task_message(
    task_id: str, author: str, content: str,
    type: str | None = None, title: str | None = None, pinned: bool = False,
) -> dict:
    async with get_db() as db:
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
        return dict(rows[0]) if rows else None


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
        task["recent_messages"] = [dict(r) for r in reversed(msg_rows)]

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

        # Backward compat: gate_retries = test_retries + review_retries
        test_retries = task.get("test_retries") or 0
        review_retries = task.get("review_retries") or 0
        task["gate_retries"] = test_retries + review_retries

        return task


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
# Search Task Messages
# ---------------------------------------------------------------------------

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
            # Create a content snippet around the match
            content = row["content"] or ""
            lower_content = content.lower()
            idx = lower_content.find(query.lower())
            if idx >= 0:
                start = max(0, idx - 50)
                end = min(len(content), idx + len(query) + 50)
                snippet = ("..." if start > 0 else "") + content[start:end] + ("..." if end < len(content) else "")
            else:
                snippet = content[:120] + ("..." if len(content) > 120 else "")
            row["snippet"] = snippet
            del row["content"]
            results.append(row)

        return results
