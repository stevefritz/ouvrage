"""Database schema initialization and migrations."""
from pathlib import Path

from switchboard.config.settings import AUTH_MODE
from switchboard.db.connection import get_db
from switchboard.db._helpers import now_iso


async def init_db():
    async with get_db() as conn:
        # Create new tables (won't affect existing ones)
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                password_hash TEXT,
                role TEXT DEFAULT 'member',
                timezone TEXT DEFAULT 'America/Toronto',
                created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                updated_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS instance (
                id INTEGER PRIMARY KEY,
                name TEXT,
                slug TEXT,
                stripe_customer_id TEXT,
                plan_tier TEXT DEFAULT 'free',
                owner_user_id INTEGER REFERENCES users(id),
                github_pat_encrypted TEXT,
                created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS user_credentials (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                anthropic_api_key TEXT,
                github_pat TEXT,
                slack_webhook_url TEXT,
                notification_preferences TEXT DEFAULT '{}',
                updated_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );

            CREATE TABLE IF NOT EXISTS api_tokens (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                token_hash TEXT NOT NULL,
                name TEXT,
                last_used_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                expires_at TIMESTAMP
            );

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

            -- OAuth Authorization Server tables
            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id TEXT PRIMARY KEY,
                client_name TEXT NOT NULL,
                client_secret_encrypted TEXT,
                redirect_uris TEXT NOT NULL DEFAULT '[]',
                grant_types TEXT NOT NULL DEFAULT '[]',
                scopes TEXT NOT NULL DEFAULT '[]',
                token_endpoint_auth_method TEXT DEFAULT 'client_secret_post',
                consent_mode TEXT DEFAULT 'implicit'
            );

            CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
                code TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id),
                redirect_uri TEXT NOT NULL,
                scope TEXT,
                code_challenge TEXT,
                code_challenge_method TEXT,
                expires_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                created_at TIMESTAMP NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                last_active TIMESTAMP NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

            CREATE TABLE IF NOT EXISTS oauth_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id),
                token_type TEXT NOT NULL DEFAULT 'Bearer',
                access_token_jti TEXT UNIQUE,
                refresh_token TEXT UNIQUE,
                scope TEXT,
                issued_at INTEGER,
                access_token_expires_at INTEGER,
                refresh_token_expires_at INTEGER,
                revoked INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                mime_type TEXT,
                size_bytes INTEGER,
                task_id TEXT REFERENCES tasks(id),
                uploaded_by INTEGER REFERENCES users(id),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS git_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                credential TEXT NOT NULL,
                hostname TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );
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

        # Migrate tasks table: add columns if missing
        task_columns = await conn.execute_fetchall("PRAGMA table_info(tasks)")
        task_col_names = [c["name"] for c in task_columns]
        if "reason" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN reason TEXT")
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
        # v5-reopen: save/restore gate state across reopen/cancel-reopen
        if "reopen_saved_gate_status" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN reopen_saved_gate_status TEXT")
        if "reopen_saved_gate_passed_at" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN reopen_saved_gate_passed_at TEXT")
        # embed-task-goals: vector embedding of task goal for semantic search
        if "embedding" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN embedding BLOB")

        # Migrate users table: add rate-limiting fields if missing
        user_columns = await conn.execute_fetchall("PRAGMA table_info(users)")
        user_col_names = [c["name"] for c in user_columns]
        if "failed_login_count" not in user_col_names:
            await conn.execute("ALTER TABLE users ADD COLUMN failed_login_count INTEGER DEFAULT 0")
        if "locked_until" not in user_col_names:
            await conn.execute("ALTER TABLE users ADD COLUMN locked_until TIMESTAMP")

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
        if "review_model" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN review_model TEXT")
        if "auto_test" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN auto_test BOOLEAN")
        if "auto_review" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN auto_review BOOLEAN")
        if "auto_pr" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN auto_pr BOOLEAN")
        if "auto_merge" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN auto_merge BOOLEAN")

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

        # Migrate projects: add created_by FK
        if "created_by" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN created_by INTEGER REFERENCES users(id)")

        # Migrate projects: add github_pat_override (encrypted, nullable)
        if "github_pat_override" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN github_pat_override TEXT")

        # Migrate projects: add display_name for human-readable project title
        if "display_name" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN display_name TEXT")

        # Migrate projects: add provider and credential_override for multi-provider support
        if "provider" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN provider TEXT")
        if "credential_override" not in project_col_names:
            await conn.execute("ALTER TABLE projects ADD COLUMN credential_override TEXT")

        # Migrate instance: add github_pat_encrypted column
        instance_columns = await conn.execute_fetchall("PRAGMA table_info(instance)")
        instance_col_names = [c["name"] for c in instance_columns]
        if "github_pat_encrypted" not in instance_col_names:
            await conn.execute("ALTER TABLE instance ADD COLUMN github_pat_encrypted TEXT")

        # Migrate components: add created_by FK
        if comp_table:
            if "created_by" not in comp_col_names:
                await conn.execute("ALTER TABLE components ADD COLUMN created_by INTEGER REFERENCES users(id)")

        # Migrate conversations: add created_by FK
        if "created_by" not in conv_col_names:
            await conn.execute("ALTER TABLE conversations ADD COLUMN created_by INTEGER REFERENCES users(id)")

        # Migrate tasks: add created_by and dispatched_by FKs
        if "created_by" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN created_by INTEGER REFERENCES users(id)")
        if "dispatched_by" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN dispatched_by INTEGER REFERENCES users(id)")

        # Migrate messages: add user_id FK
        if "user_id" not in msg_col_names:
            await conn.execute("ALTER TABLE messages ADD COLUMN user_id INTEGER REFERENCES users(id)")

        # Migrate task_messages (messages with task_id): same user_id FK — already handled above
        # (messages table serves both conversation messages and task messages)

        # Migrate push_subscriptions: add user_id FK
        push_columns = await conn.execute_fetchall("PRAGMA table_info(push_subscriptions)")
        push_col_names = [c["name"] for c in push_columns]
        if "user_id" not in push_col_names:
            await conn.execute("ALTER TABLE push_subscriptions ADD COLUMN user_id INTEGER REFERENCES users(id)")

        # Migrate files: add task_id and project_id FKs
        files_table = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='files'"
        )
        if files_table:
            files_columns = await conn.execute_fetchall("PRAGMA table_info(files)")
            files_col_names = [c["name"] for c in files_columns]
            if "task_id" not in files_col_names:
                await conn.execute("ALTER TABLE files ADD COLUMN task_id TEXT REFERENCES tasks(id)")
            if "project_id" not in files_col_names:
                await conn.execute("ALTER TABLE files ADD COLUMN project_id TEXT REFERENCES projects(id)")

        # Migrate api_tokens: add token_prefix for display in the UI
        token_columns = await conn.execute_fetchall("PRAGMA table_info(api_tokens)")
        token_col_names = [c["name"] for c in token_columns]
        if "token_prefix" not in token_col_names:
            await conn.execute("ALTER TABLE api_tokens ADD COLUMN token_prefix TEXT")

        # ---------------------------------------------------------------------------
        # Bootstrap migration: seed default owner user and instance if users is empty.
        # Backfill all FK columns so existing rows point to the owner.
        # This runs once on upgrade from single-tenant to user-model schema.
        # ---------------------------------------------------------------------------
        user_count_rows = await conn.execute_fetchall("SELECT COUNT(*) as cnt FROM users")
        if user_count_rows[0]["cnt"] == 0:
            ts = now_iso()
            if AUTH_MODE == "saas":
                # SaaS mode: create instance row without a placeholder user.
                # The real owner is created via POST /internal/bootstrap-user
                # during provisioning, which also sets owner_user_id.
                inst_rows = await conn.execute_fetchall("SELECT COUNT(*) as cnt FROM instance")
                if inst_rows[0]["cnt"] == 0:
                    await conn.execute(
                        """INSERT INTO instance (id, name, slug, plan_tier, owner_user_id, created_at)
                           VALUES (1, ?, ?, ?, NULL, ?)""",
                        ("Ouvrage", "default", "free", ts),
                    )
            else:
                # Local mode: seed a default owner user for single-tenant setups
                cursor = await conn.execute(
                    """INSERT INTO users (email, name, role, timezone, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    ("owner@localhost", "Owner", "owner", "America/Toronto", ts, ts),
                )
                user_id = cursor.lastrowid

                await conn.execute(
                    """INSERT INTO instance (id, name, slug, plan_tier, owner_user_id, created_at)
                       VALUES (1, ?, ?, ?, ?, ?)""",
                    ("Ouvrage", "default", "free", user_id, ts),
                )

                # Backfill FK columns on existing rows
                await conn.execute(
                    "UPDATE projects SET created_by = ? WHERE created_by IS NULL", (user_id,)
                )
                await conn.execute(
                    "UPDATE components SET created_by = ? WHERE created_by IS NULL", (user_id,)
                )
                await conn.execute(
                    "UPDATE conversations SET created_by = ? WHERE created_by IS NULL", (user_id,)
                )
                await conn.execute(
                    "UPDATE tasks SET created_by = ?, dispatched_by = ? WHERE created_by IS NULL",
                    (user_id, user_id),
                )
                # Only backfill messages authored by humans (not system actors)
                await conn.execute(
                    """UPDATE messages SET user_id = ?
                       WHERE author NOT IN ('dispatcher', 'cc-worker', 'switchboard')
                       AND user_id IS NULL""",
                    (user_id,),
                )

        # vec0 virtual tables for vector similarity search (sqlite-vec)
        vec_tables = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('messages_vec', 'tasks_vec', 'chunks_vec')"
        )
        vec_table_names = {r["name"] for r in vec_tables}

        if "messages_vec" not in vec_table_names:
            try:
                await conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_vec USING vec0(embedding float[1536])"
                )
            except Exception:
                pass  # sqlite-vec not available

        if "tasks_vec" not in vec_table_names:
            try:
                await conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS tasks_vec USING vec0(embedding float[1536])"
                )
            except Exception:
                pass

        if "chunks_vec" not in vec_table_names:
            try:
                await conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(embedding float[1536])"
                )
            except Exception:
                pass

        # FTS5 virtual tables for full-text search
        fts_tables = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('messages_fts', 'tasks_fts')"
        )
        fts_table_names = {r["name"] for r in fts_tables}

        if "messages_fts" not in fts_table_names:
            await conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                    USING fts5(content, content='messages', content_rowid='id');
            """)

        if "tasks_fts" not in fts_table_names:
            await conn.executescript("""
                CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts
                    USING fts5(goal, content='tasks', content_rowid='rowid');
            """)

        # FTS5 sync triggers — drop before recreating so updated definitions take effect.
        # messages_fts_insert: skip NULL content (avoids FTS null-token errors).
        # messages_fts_update: scoped to content column only (avoids spurious FTS churn).
        # tasks_fts_update: scoped to goal column only.
        await conn.executescript("""
            DROP TRIGGER IF EXISTS messages_fts_insert;
            DROP TRIGGER IF EXISTS messages_fts_update;
            DROP TRIGGER IF EXISTS tasks_fts_update;

            CREATE TRIGGER IF NOT EXISTS messages_fts_insert
                AFTER INSERT ON messages WHEN new.content IS NOT NULL BEGIN
                    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
                END;

            CREATE TRIGGER IF NOT EXISTS messages_fts_delete
                AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
                END;

            CREATE TRIGGER IF NOT EXISTS messages_fts_update
                AFTER UPDATE OF content ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
                    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
                END;

            CREATE TRIGGER IF NOT EXISTS tasks_fts_insert
                AFTER INSERT ON tasks BEGIN
                    INSERT INTO tasks_fts(rowid, goal) VALUES (new.rowid, new.goal);
                END;

            CREATE TRIGGER IF NOT EXISTS tasks_fts_delete
                AFTER DELETE ON tasks BEGIN
                    INSERT INTO tasks_fts(tasks_fts, rowid, goal) VALUES ('delete', old.rowid, old.goal);
                END;

            CREATE TRIGGER IF NOT EXISTS tasks_fts_update
                AFTER UPDATE OF goal ON tasks BEGIN
                    INSERT INTO tasks_fts(tasks_fts, rowid, goal) VALUES ('delete', old.rowid, old.goal);
                    INSERT INTO tasks_fts(rowid, goal) VALUES (new.rowid, new.goal);
                END;
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
            CREATE INDEX IF NOT EXISTS idx_task_tags ON task_tags(task_id);
            CREATE INDEX IF NOT EXISTS idx_task_tags_tag ON task_tags(tag);
            DROP INDEX IF EXISTS idx_msg_content;
            CREATE INDEX IF NOT EXISTS idx_component_project ON components(project_id);
            CREATE INDEX IF NOT EXISTS idx_task_component ON tasks(component_id);
            CREATE INDEX IF NOT EXISTS idx_punchlist_component ON punchlist(component_id);
            CREATE INDEX IF NOT EXISTS idx_punchlist_claimed_by ON punchlist(claimed_by);

            CREATE TABLE IF NOT EXISTS message_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                chunk_index INTEGER NOT NULL,
                heading TEXT,
                content TEXT NOT NULL,
                embedding BLOB,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_message_chunks_message_id ON message_chunks(message_id);
            CREATE INDEX IF NOT EXISTS idx_api_tokens_user ON api_tokens(user_id);

            CREATE TABLE IF NOT EXISTS task_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                action TEXT NOT NULL,
                triggered_by TEXT NOT NULL,
                source_detail TEXT,
                previous_status TEXT,
                new_status TEXT,
                created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_audit_log_task ON task_audit_log(task_id, created_at);

            CREATE TABLE IF NOT EXISTS instance_config (
                id INTEGER PRIMARY KEY,
                concurrency_limit INTEGER,
                max_projects INTEGER,
                trial_ends_at TEXT
            );

            CREATE TABLE IF NOT EXISTS task_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                session_id TEXT,
                started_at TIMESTAMP DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                finished_at TIMESTAMP,
                outcome TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                UNIQUE(task_id, attempt_number)
            );
            CREATE INDEX IF NOT EXISTS idx_task_attempts_task ON task_attempts(task_id, attempt_number);
        """)

        # vec0 delete triggers — keep vec0 tables in sync when rows are deleted.
        # Created here (after message_chunks is created) so the trigger reference to
        # message_chunks is valid. Only created when all three vec0 tables exist.
        # ON DELETE CASCADE from messages→message_chunks fires AFTER DELETE triggers on
        # message_chunks, so chunks_vec_delete fires correctly on cascade deletes.
        vec_tables_for_triggers = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('messages_vec', 'tasks_vec', 'chunks_vec')"
        )
        if len(vec_tables_for_triggers) == 3:
            try:
                await conn.executescript("""
                    CREATE TRIGGER IF NOT EXISTS messages_vec_delete
                        AFTER DELETE ON messages BEGIN
                            DELETE FROM messages_vec WHERE rowid = old.id;
                        END;

                    CREATE TRIGGER IF NOT EXISTS tasks_vec_delete
                        AFTER DELETE ON tasks BEGIN
                            DELETE FROM tasks_vec WHERE rowid = old.rowid;
                        END;

                    CREATE TRIGGER IF NOT EXISTS chunks_vec_delete
                        AFTER DELETE ON message_chunks BEGIN
                            DELETE FROM chunks_vec WHERE rowid = old.id;
                        END;
                """)
            except Exception:
                pass  # sqlite-vec not available at trigger-creation time

        # Credential encryption migration: encrypt any plaintext values in user_credentials.
        # Only runs if SWITCHBOARD_MASTER_KEY is set — skipped silently otherwise.
        import os as _os
        if _os.environ.get("SWITCHBOARD_MASTER_KEY"):
            from switchboard.crypto import maybe_encrypt, is_fernet_token, encrypt_value, decrypt_value
            cred_rows = await conn.execute_fetchall(
                "SELECT user_id, anthropic_api_key, github_pat FROM user_credentials"
            )
            for row in cred_rows:
                updates = {}
                for field in ("anthropic_api_key", "github_pat"):
                    val = row[field]
                    if val and not is_fernet_token(val):
                        updates[field] = maybe_encrypt(val)
                if updates:
                    set_clause = ", ".join(f"{k} = ?" for k in updates)
                    await conn.execute(
                        f"UPDATE user_credentials SET {set_clause} WHERE user_id = ?",
                        list(updates.values()) + [row["user_id"]],
                    )

            # PAT migration: if instance.github_pat_encrypted is null but owner's
            # user_credentials.github_pat is set, copy it to instance level.
            inst_rows = await conn.execute_fetchall(
                "SELECT id, owner_user_id, github_pat_encrypted FROM instance LIMIT 1"
            )
            if inst_rows:
                inst = inst_rows[0]
                if not inst["github_pat_encrypted"] and inst["owner_user_id"]:
                    owner_cred = await conn.execute_fetchall(
                        "SELECT github_pat FROM user_credentials WHERE user_id = ?",
                        (inst["owner_user_id"],),
                    )
                    if owner_cred and owner_cred[0]["github_pat"]:
                        raw_pat = owner_cred[0]["github_pat"]
                        # Decrypt if already encrypted, then re-encrypt for instance column
                        if is_fernet_token(raw_pat):
                            raw_pat = decrypt_value(raw_pat)
                        encrypted = encrypt_value(raw_pat)
                        await conn.execute(
                            "UPDATE instance SET github_pat_encrypted = ? WHERE id = ?",
                            (encrypted, inst["id"]),
                        )
                        import logging as _logging
                        _logging.getLogger(__name__).info(
                            "Migrated GitHub PAT from user credentials to instance level."
                        )

        # Auto-migrate instance.github_pat_encrypted → git_credentials row
        # If the instance has a GitHub PAT but no git_credentials row for github, create one.
        if _os.environ.get("SWITCHBOARD_MASTER_KEY"):
            inst_rows_gc = await conn.execute_fetchall(
                "SELECT github_pat_encrypted FROM instance WHERE id = 1"
            )
            if inst_rows_gc and inst_rows_gc[0]["github_pat_encrypted"]:
                existing_gc = await conn.execute_fetchall(
                    "SELECT id FROM git_credentials WHERE provider = 'github' LIMIT 1"
                )
                if not existing_gc:
                    await conn.execute(
                        """INSERT INTO git_credentials (provider, credential, hostname, created_at)
                           VALUES ('github', ?, 'github.com', ?)""",
                        (inst_rows_gc[0]["github_pat_encrypted"], now_iso()),
                    )
                    import logging as _logging_gc
                    _logging_gc.getLogger(__name__).info(
                        "Migrated instance GitHub PAT to git_credentials table."
                    )

            # Auto-migrate project github_pat_override → credential_override
            proj_migrate_rows = await conn.execute_fetchall(
                "SELECT id, github_pat_override FROM projects WHERE github_pat_override IS NOT NULL AND credential_override IS NULL"
            )
            for row in proj_migrate_rows:
                await conn.execute(
                    "UPDATE projects SET credential_override = ? WHERE id = ?",
                    (row["github_pat_override"], row["id"]),
                )

        # Migrate task_attempts: backfill from existing tasks with session_id
        attempts_table = await conn.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_attempts'"
        )
        if attempts_table:
            # Backfill: for any task that has a session_id but no attempt record,
            # create an attempt row for the current_attempt
            backfill_rows = await conn.execute_fetchall(
                """SELECT t.id, t.current_attempt, t.session_id
                   FROM tasks t
                   LEFT JOIN task_attempts ta ON t.id = ta.task_id AND t.current_attempt = ta.attempt_number
                   WHERE t.session_id IS NOT NULL AND ta.id IS NULL"""
            )
            for row in backfill_rows:
                await conn.execute(
                    """INSERT OR IGNORE INTO task_attempts (task_id, attempt_number, session_id, started_at)
                       VALUES (?, ?, ?, ?)""",
                    (row["id"], row["current_attempt"] or 1, row["session_id"], now_iso()),
                )

        # Migrate projects: normalize SSH repo URLs to HTTPS
        # git@github.com:owner/repo.git → https://github.com/owner/repo.git
        import re as _re
        import logging as _logging2
        _ssh_pat = _re.compile(r"^git@github\.com:([^/]+)/(.+?)(?:\.git)?$")
        _schema_log = _logging2.getLogger(__name__)
        project_rows = await conn.execute_fetchall("SELECT id, repo, working_dir FROM projects")
        for row in project_rows:
            m = _ssh_pat.match(row["repo"])
            if m:
                owner, repo_name = m.group(1), m.group(2)
                https_url = f"https://github.com/{owner}/{repo_name}.git"
                await conn.execute(
                    "UPDATE projects SET repo = ? WHERE id = ?",
                    (https_url, row["id"]),
                )
                _schema_log.info(f"Migrated repo URL for project '{row['id']}': {row['repo']} → {https_url}")
                # Also update the bare repo remote if it exists
                bare_path = _os.path.join(row["working_dir"], ".bare")
                if _os.path.exists(bare_path):
                    import asyncio as _asyncio
                    proc = await _asyncio.create_subprocess_exec(
                        "git", "-C", bare_path, "remote", "set-url", "origin", https_url,
                        stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()
                    if proc.returncode == 0:
                        _schema_log.info(f"Updated bare repo remote for project '{row['id']}' to {https_url}")
                    else:
                        _schema_log.warning(f"Failed to update bare repo remote for '{row['id']}': {stderr.decode().strip()}")

        # Migrate instance_config: add trial_ends_at column if missing
        ic_cols = await conn.execute_fetchall("PRAGMA table_info(instance_config)")
        ic_col_names = {row["name"] for row in ic_cols}
        if "trial_ends_at" not in ic_col_names:
            await conn.execute("ALTER TABLE instance_config ADD COLUMN trial_ends_at TEXT")

        await conn.commit()

    # Ensure uploads directory exists (worker-accessible, outside /data)
    from switchboard.config.settings import UPLOADS_DIR, DB_PATH
    import logging as _logging_uploads
    import shutil
    _uploads_log = _logging_uploads.getLogger(__name__)

    uploads_dir = Path(UPLOADS_DIR)
    uploads_dir.mkdir(parents=True, exist_ok=True)

    # Migrate uploads from old location (/data/uploads/) if present
    old_uploads = Path(DB_PATH).parent / "uploads"
    if old_uploads.exists() and old_uploads.resolve() != uploads_dir.resolve():
        async with get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT id, stored_path FROM files WHERE stored_path LIKE ?",
                (str(old_uploads) + "%",),
            )
            for row in rows:
                old_path = Path(row["stored_path"])
                new_path = uploads_dir / old_path.relative_to(old_uploads)
                if old_path.exists():
                    new_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(old_path), str(new_path))
                await conn.execute(
                    "UPDATE files SET stored_path = ? WHERE id = ?",
                    (str(new_path), row["id"]),
                )
            if rows:
                await conn.commit()
                _uploads_log.info("Migrated %d files from %s to %s", len(rows), old_uploads, uploads_dir)
        # Clean up empty old directory
        try:
            shutil.rmtree(str(old_uploads))
        except OSError:
            pass
