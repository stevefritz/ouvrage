# rename_project Implementation Plan

## Overview

Atomically rename a project ID, cascading through every database reference and renaming the on-disk working directory. The project ID is embedded in task IDs (`{project_id}/{task_slug}`), foreign keys, and file paths.

---

## 1. Database Tables Requiring Updates

### Direct project_id references (7 tables)

| # | Table | Column | Type | Notes |
|---|-------|--------|------|-------|
| 1 | `projects` | `id` | PK | The root — rename this first (temporarily disable FK) |
| 2 | `tasks` | `project_id` | FK → projects(id) | Direct FK |
| 3 | `tasks` | `id` | PK (TEXT) | Format: `{project_id}/{slug}` — needs string replacement |
| 4 | `tasks` | `depends_on` | TEXT | Stores another task ID — needs replacement if it belongs to this project |
| 5 | `tasks` | `parent_task_id` | TEXT | Stores another task ID — needs replacement if it belongs to this project |
| 6 | `components` | `project_id` | FK → projects(id) | Direct FK |
| 7 | `conversations` | `project` | TEXT (not named project_id) | Direct FK equivalent |
| 8 | `files` | `project_id` | FK → projects(id) | Direct FK |
| 9 | `files` | `task_id` | FK → tasks(id) | Cascades from task ID rename |

### Cascading from task ID rename (8 tables)

These tables reference `tasks.id` and must be updated when task IDs change:

| # | Table | Column | Notes |
|---|-------|--------|-------|
| 10 | `task_checklist` | `task_id` | FK → tasks(id) |
| 11 | `task_artifacts` | `task_id` | FK → tasks(id) |
| 12 | `task_tags` | `task_id` | FK → tasks(id) |
| 13 | `subtasks` | `task_id` | FK → tasks(id) |
| 14 | `messages` | `task_id` | FK → tasks(id) — task messages (not conversation messages) |
| 15 | `task_audit_log` | `task_id` | TEXT, no FK constraint |
| 16 | `task_attempts` | `task_id` | FK → tasks(id) ON DELETE CASCADE |
| 17 | `punchlist` | `claimed_by` | TEXT — stores task ID of claiming task |
| 18 | `punchlist` | `resolved_by` | TEXT — stores task ID of resolving task |

### Virtual tables (NO update needed)

| Table | Why safe |
|-------|----------|
| `messages_vec` | Keyed by `rowid` (integer), not task_id or project_id |
| `tasks_vec` | Keyed by `rowid` (integer) |
| `chunks_vec` | Keyed by `rowid` (integer) — linked to `message_chunks.id` |
| `messages_fts` | Content table synced via triggers on `messages` — uses `rowid` |
| `tasks_fts` | Content table synced via triggers on `tasks` — uses `rowid` |
| `message_chunks` | FK → `messages(id)` with `ON DELETE CASCADE` — no task_id/project_id column |

### Tables with NO project reference (safe to ignore)

`users`, `user_credentials`, `api_tokens`, `instance`, `instance_config`, `oauth_clients`, `oauth_authorization_codes`, `oauth_tokens`, `sessions`, `push_subscriptions`, `notification_settings`, `component_conversations`

---

## 2. Foreign Key / Cascade Analysis

SQLite FKs in this schema use **no ON UPDATE CASCADE**. The only CASCADE declarations are:
- `message_chunks.message_id` → `messages(id)` ON DELETE CASCADE
- `task_attempts.task_id` → `tasks(id)` ON DELETE CASCADE

This means we **cannot** simply UPDATE `projects.id` and have it cascade. We must:
1. Temporarily disable FK enforcement (`PRAGMA foreign_keys = OFF`)
2. Run all UPDATEs in a single transaction
3. Re-enable FK enforcement
4. Run `PRAGMA foreign_key_check` to verify integrity

---

## 3. The Atomic Rename Transaction

### SQL Statements (in order)

```sql
-- Pre-check: reject if new_id already exists
SELECT id FROM projects WHERE id = ?;  -- must not exist

-- Pre-check: reject if any tasks are in active states
SELECT id, status FROM tasks WHERE project_id = ? AND status IN ('working', 'dispatching', 'testing', 'reviewing');
-- If any rows: raise error, refuse rename

PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;

-- 1. Rename project primary key
UPDATE projects SET id = :new_id WHERE id = :old_id;

-- 2. Update projects.working_dir (path changes from /work/{old_id} to /work/{new_id})
UPDATE projects SET working_dir = REPLACE(working_dir, :old_id, :new_id) WHERE id = :new_id;

-- 3. Update tasks.project_id
UPDATE tasks SET project_id = :new_id WHERE project_id = :old_id;

-- 4. Rename task IDs: replace project prefix in compound IDs
--    Task IDs are formatted as '{project_id}/{slug}'
--    Replace '{old_id}/' with '{new_id}/' at the start of the ID
UPDATE tasks SET id = :new_id || substr(id, length(:old_id) + 1)
    WHERE id LIKE :old_id || '/%';

-- 5. Update worktree_path on tasks (if any are set)
UPDATE tasks SET worktree_path = REPLACE(worktree_path, '/' || :old_id || '/', '/' || :new_id || '/')
    WHERE worktree_path LIKE '%/' || :old_id || '/%';

-- 6. Cascade task ID renames through all referencing tables
--    For each table that stores task_id, replace the project prefix
UPDATE task_checklist SET task_id = :new_id || substr(task_id, length(:old_id) + 1)
    WHERE task_id LIKE :old_id || '/%';

UPDATE task_artifacts SET task_id = :new_id || substr(task_id, length(:old_id) + 1)
    WHERE task_id LIKE :old_id || '/%';

UPDATE task_tags SET task_id = :new_id || substr(task_id, length(:old_id) + 1)
    WHERE task_id LIKE :old_id || '/%';

UPDATE subtasks SET task_id = :new_id || substr(task_id, length(:old_id) + 1)
    WHERE task_id LIKE :old_id || '/%';

UPDATE messages SET task_id = :new_id || substr(task_id, length(:old_id) + 1)
    WHERE task_id LIKE :old_id || '/%';

UPDATE task_audit_log SET task_id = :new_id || substr(task_id, length(:old_id) + 1)
    WHERE task_id LIKE :old_id || '/%';

UPDATE task_attempts SET task_id = :new_id || substr(task_id, length(:old_id) + 1)
    WHERE task_id LIKE :old_id || '/%';

-- 7. Update cross-references within tasks table
UPDATE tasks SET depends_on = :new_id || substr(depends_on, length(:old_id) + 1)
    WHERE depends_on LIKE :old_id || '/%';

UPDATE tasks SET parent_task_id = :new_id || substr(parent_task_id, length(:old_id) + 1)
    WHERE parent_task_id LIKE :old_id || '/%';

-- 8. Update punchlist task ID references
UPDATE punchlist SET claimed_by = :new_id || substr(claimed_by, length(:old_id) + 1)
    WHERE claimed_by LIKE :old_id || '/%';

UPDATE punchlist SET resolved_by = :new_id || substr(resolved_by, length(:old_id) + 1)
    WHERE resolved_by LIKE :old_id || '/%';

-- 9. Update conversations.project
UPDATE conversations SET project = :new_id WHERE project = :old_id;

-- 10. Update files.project_id
UPDATE files SET project_id = :new_id WHERE project_id = :old_id;

-- 11. Update files.task_id (cascading from task rename)
UPDATE files SET task_id = :new_id || substr(task_id, length(:old_id) + 1)
    WHERE task_id LIKE :old_id || '/%';

-- 12. Update components.project_id
UPDATE components SET project_id = :new_id WHERE project_id = :old_id;

COMMIT;
PRAGMA foreign_keys = ON;

-- Verify FK integrity
PRAGMA foreign_key_check;
```

**Important:** The `LIKE :old_id || '/%'` pattern safely matches only task IDs that start with the old project ID followed by a slash. The `substr(id, length(:old_id) + 1)` extracts everything from the `/slug` onward, preserving the slash and slug.

---

## 4. Disk Operations

### Working directory rename

The project's `working_dir` is typically `/work/{project_id}` (a bare repo clone + worktree directories).

```python
import os, shutil

old_dir = project["working_dir"]  # e.g., /work/mcp-switchboard
new_dir = old_dir.replace(old_id, new_id)  # e.g., /work/ouvrage

# Pre-check: ensure no active worktrees have open processes
# (handled by rejecting rename when active tasks exist)

os.rename(old_dir, new_dir)
```

**Critical:** `os.rename()` is atomic on the same filesystem. The working directory contains:
- `.bare/` — the bare git clone
- `{task-slug}/` — individual task worktrees
- `.task-history/` — archived session logs

All of these move together with the parent directory rename.

### Git worktree metadata

After renaming the directory, git worktree metadata (`.bare/worktrees/*/gitdir`) contains absolute paths that will be stale. However, since we require all active tasks to be completed/cancelled before rename, there should be no active worktrees. The `.bare/worktrees/` entries for completed tasks whose worktrees were released are already cleaned up.

**If any worktrees exist**, their `.git` files and the bare repo's `worktrees/*/gitdir` files need path fixup:
```python
# For each worktree dir that still exists under new_dir:
for wt_name in os.listdir(os.path.join(new_dir, ".bare", "worktrees")):
    gitdir_path = os.path.join(new_dir, ".bare", "worktrees", wt_name, "gitdir")
    if os.path.exists(gitdir_path):
        content = open(gitdir_path).read()
        open(gitdir_path, "w").write(content.replace(old_dir, new_dir))

# For each task worktree dir:
for entry in os.listdir(new_dir):
    dot_git = os.path.join(new_dir, entry, ".git")
    if os.path.isfile(dot_git):
        content = open(dot_git).read()
        open(dot_git, "w").write(content.replace(old_dir, new_dir))
```

---

## 5. MCP Tool Interface

### Tool definition (`server/tools.py`)

```python
Tool(
    name="rename_project",
    description="Rename a project ID, cascading through all task IDs, database references, and disk paths. Rejects if any tasks are active.",
    inputSchema={
        "type": "object",
        "properties": {
            "old_id": {"type": "string", "description": "Current project ID"},
            "new_id": {"type": "string", "description": "New project ID (lowercase alphanumeric + hyphens)"},
        },
        "required": ["old_id", "new_id"],
    },
)
```

### Handler location

Create handler in `switchboard/server/handlers/projects.py`:
```python
async def _handle_rename_project(arguments: dict) -> dict:
```

Register in `switchboard/server/dispatch.py`:
```python
"rename_project": _handle_rename_project,
```

### Handler logic

1. Validate `new_id` format: `re.match(r'^[a-z0-9][a-z0-9-]*$', new_id)`
2. Verify `old_id` exists
3. Verify `new_id` doesn't already exist
4. Check for active tasks — reject if any in `working`, `dispatching`, `testing`, `reviewing`
5. Run the SQL transaction (all 12 UPDATE groups)
6. Rename the working directory on disk
7. Fix git worktree metadata paths
8. Return `{"renamed": True, "old_id": old_id, "new_id": new_id, "tasks_updated": count}`

### Dashboard API route

Add to `switchboard/dashboard/api.py`:
```python
elif path.startswith("/dashboard/api/projects/") and method == "POST" and rest.endswith("/rename"):
    return await _handle_rename_project_api(receive, send, project_id)
```

This calls the same core logic as the MCP handler.

---

## 6. Edge Cases and Guardrails

### Must reject rename if:
- **Active tasks exist** — any task in `working`, `dispatching`, `testing`, `reviewing` status. These tasks have running CC sessions or worktrees that reference the old path.
- **New ID already exists** — another project with that ID.
- **New ID is invalid** — must match `^[a-z0-9][a-z0-9-]*$`.
- **Old ID doesn't exist** — obvious.

### Document as limitations (don't try to fix):
- **Open GitHub PRs** reference old branch names. Branch names don't contain the project ID (they use just the task slug), so PR branches are fine. However, PR titles and descriptions may contain old task IDs — these are external and cannot be renamed.
- **Message content** — pinned specs, progress updates, and handoff messages contain task IDs in their text content. These are historical records and should NOT be bulk-replaced (too risky, could corrupt markdown). Document that old task IDs in message content are historical artifacts.
- **Git branch names** — branches use the task slug (e.g., `plan-rename-project`), NOT the full task ID. No branch renaming needed.
- **External references** — Jira tickets, Slack threads, bookmarks that reference old task IDs will break. This is inherent to any rename operation.

### Queued tasks
Tasks in `ready` or `queued` status are safe to rename — they have no running sessions or worktrees.

### Held tasks
Tasks in `held` status are paused but may have worktrees. Since we require no `working` tasks, held tasks with `worktree_path` set need their worktree paths updated in the DB (handled by the UPDATE on `tasks.worktree_path`). The actual directory rename handles the filesystem side.

---

## 7. Files That Need Changes

| File | Change |
|------|--------|
| `switchboard/server/tools.py` | Add `rename_project` tool schema |
| `switchboard/server/dispatch.py` | Register handler |
| `switchboard/server/handlers/projects.py` | Implement `_handle_rename_project` |
| `switchboard/db/projects.py` | Add `rename_project()` DB function with the full transaction |
| `switchboard/dashboard/api.py` | Add REST endpoint for dashboard rename button |
| `tests/test_rename_project.py` | New test file for the rename feature |

---

## 8. Implementation Notes for Sonnet

1. **PRAGMA foreign_keys = OFF is connection-scoped.** Since we use `async with get_db() as db:`, the pragma only affects that connection. Re-enable it before exiting.

2. **The LIKE pattern is safe.** `old_id || '/%'` will not accidentally match a project like `mcp-switchboard-v2` because the `/` delimiter is required.

3. **Use `db.execute()` not `db.executescript()`** for the transaction. `executescript` implicitly commits, which would break atomicity.

4. **Test the transaction with rollback.** Write a test that renames, verifies all references, then also tests that a failed rename (e.g., duplicate new_id) rolls back cleanly.

5. **The disk rename should happen AFTER the DB commit succeeds.** If the DB transaction fails, don't touch disk. If disk rename fails after DB commit, the handler should attempt to reverse the DB changes (or log an error for manual cleanup).

6. **Order matters:** Rename `projects.id` first (step 1), then update all FK references. Since FKs are disabled, this works. Renaming tasks.id before updating child tables also works because FKs are off.

7. **`delete_project` in `db/projects.py` is a useful reference** — it already handles the full cascade of child record deletion. The rename function follows the same pattern but with UPDATEs instead of DELETEs.
