"""Database schema initialization and migrations."""
from switchboard.db.connection import get_db


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
        # v5-reopen: save/restore gate state across reopen/cancel-reopen
        if "reopen_saved_gate_status" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN reopen_saved_gate_status TEXT")
        if "reopen_saved_gate_passed_at" not in task_col_names:
            await conn.execute("ALTER TABLE tasks ADD COLUMN reopen_saved_gate_passed_at TEXT")

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
