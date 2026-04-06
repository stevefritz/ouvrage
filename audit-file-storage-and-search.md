# Audit: File Storage + Search/Chunking Pipeline

## 1. File Storage

### 1.1 `files` Table Schema

**Location:** `switchboard/db/schema.py:256-266`

```sql
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,          -- UUID v4 (generated in handler)
    filename TEXT NOT NULL,       -- Display name (basename only, path components stripped)
    stored_path TEXT NOT NULL,    -- Absolute path on disk to the stored copy
    mime_type TEXT,               -- MIME type based on extension (e.g. 'text/markdown')
    size_bytes INTEGER,           -- File size in bytes
    task_id TEXT REFERENCES tasks(id),      -- FK to owning task (NULL for project-level files)
    project_id TEXT REFERENCES projects(id), -- FK to project (set on promote or add_project_file)
    uploaded_by INTEGER REFERENCES users(id), -- Always NULL for worker-uploaded files
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP          -- Set on promote/update operations
);
```

**Migration note:** `task_id` and `project_id` were added via `ALTER TABLE` migration (`schema.py:508-518`), not in the original CREATE TABLE. They exist on all current installations.

### 1.2 How `file_id` Works

The `id` column is a UUID v4 string generated in the handler at upload time:

```python
file_id = str(uuid.uuid4())  # files_handler.py:139
```

This UUID is also used as the directory name on disk (`uploads/{uuid}/filename`).

### 1.3 Disk Storage Layout

Files are stored under the **uploads directory**, which is sibling to the SQLite database file:

```python
def _uploads_dir() -> Path:
    from switchboard.config.settings import DB_PATH
    return Path(DB_PATH).parent / "uploads"
```

For a typical deployment with `DB_PATH = ./data/switchboard.db`, files live at:

```
data/uploads/{uuid}/          -- Directory named by file UUID
data/uploads/{uuid}/report.md -- Actual file (original filename preserved)
```

The handler copies the source file from the worktree into this permanent location using `shutil.copy2()`, preserving metadata.

### 1.4 `add_task_file` — Full Flow

**Location:** `switchboard/server/handlers/files_handler.py:89-161`

1. **Worker-only gate:** Rejects non-worker callers (`get_request_is_worker()` check).
2. **Validate task exists:** Looks up task by `task_id`, requires `worktree_path` to be set.
3. **Security check:** Resolves real paths and verifies the source file is within the task's worktree (prevents directory traversal via symlinks or `..`).
4. **Extension whitelist:** Only files with extensions in `ALLOWED_EXTENSIONS` are accepted:
   - Images: `png, jpg, jpeg, gif, webp, svg`
   - Text: `txt, md, json, csv, yaml, yml, toml, xml`
   - Documents: `pdf`
5. **Size limit:** Rejects files > 10MB (`MAX_FILE_SIZE = 10 * 1024 * 1024`).
6. **Copy to permanent storage:**
   ```python
   dest_dir = _uploads_dir() / file_id   # e.g. data/uploads/abc123-...
   dest_dir.mkdir(parents=True, exist_ok=True)
   dest = dest_dir / filename
   shutil.copy2(str(real_src), str(dest))
   ```
7. **Create DB record:** Calls `db.create_file()` with the UUID, filename, stored path, MIME type, size, and `task_id`.
8. **Returns:** `{id, filename, stored_path, size_bytes}`.

### 1.5 `get_file` — Reading Files Back

**Two tools exist:**

1. **`get_file`** (`files_handler.py:228-273`) — The primary tool. Accepts `id` parameter.
   - Looks up the DB record by `file_id`
   - For non-readable files (images, PDFs): returns metadata only (`readable: false`)
   - For readable text files: reads `stored_path` from disk, returns content (up to `max_bytes`, default 1MB)
   - Returns: `{id, filename, content, size_bytes, truncated, mime_type, task_id, project_id, created_at, readable}`

2. **`get_attached_file`** (`files_handler.py:50-86`) — Deprecated alias with slightly different response shape.

**Readable extensions:** `txt, md, json, csv, yaml, yml, toml, xml`

### 1.6 `promote_task_file`

**Location:** `files_handler.py:276-298`, `db/files.py:57-68`

Promotes a task-level file to also appear at the project level by setting `project_id`:

```sql
UPDATE files SET project_id = ?, updated_at = ? WHERE id = ? AND task_id IS NOT NULL
```

The file retains its `task_id` — it appears in both task and project file listings. Only files with a `task_id` can be promoted (prevents double-promoting project-only files).

### 1.7 File Delete Flow

**There is no automatic file cleanup on task close/cancel.** Files persist until explicitly deleted.

#### Explicit delete via dashboard
**Location:** `dashboard/api.py:1956-1972`

1. Authenticated user calls `DELETE /dashboard/api/files/{file_id}`
2. Handler looks up the DB record
3. Removes the UUID directory from disk: `shutil.rmtree(uuid_dir, ignore_errors=True)`
4. Deletes the DB row: `db.delete_file(file_id)` → `DELETE FROM files WHERE id = ?`

#### Project delete cascade
**Location:** `db/projects.py:116-141`

When a project is deleted, all files belonging to its tasks are deleted from the DB:
```python
await db.execute(f"DELETE FROM files WHERE task_id IN ({placeholders})", task_ids)
```

**Important gap:** The `delete_project` function explicitly notes "Does NOT remove files from disk — callers are responsible for cleanup." This means project deletion orphans files on disk.

#### No cascade on task delete
The `files` table has a `REFERENCES tasks(id)` FK on `task_id`, but **without `ON DELETE CASCADE`**. If a task row is deleted directly, the file rows become orphaned (task_id points to a nonexistent task). However, the `delete_project` function handles this manually.

---

## 2. Message Chunking + Embedding Pipeline

### 2.1 `message_chunks` Table Schema

**Location:** `switchboard/db/schema.py:678-688`

```sql
CREATE TABLE IF NOT EXISTS message_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- Used as rowid key for chunks_vec
    message_id INTEGER NOT NULL,           -- FK to source message
    chunk_index INTEGER NOT NULL,          -- Position in message (-1 = sentinel)
    heading TEXT,                          -- Markdown heading text (NULL for non-headed sections)
    content TEXT NOT NULL,                 -- Full text of this chunk
    embedding BLOB,                        -- Packed float32[1536] vector (NULL if embedding failed)
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_message_chunks_message_id ON message_chunks(message_id);
```

**Key details:**
- `ON DELETE CASCADE` — when a message is deleted, its chunks are automatically removed
- `chunk_index = -1` is a **sentinel row** — indicates the message was processed but produced no useful chunks (too short, no headers, or single section)
- The `embedding` column stores packed float32 blobs (1536 dims × 4 bytes = 6144 bytes per vector)

#### How task_id/project_id scoping works for chunks

Chunks do **not** have their own `task_id` or `project_id` columns. Scoping is achieved by joining through the parent message:

```sql
-- In search_message_chunks() (db/search.py:549-551):
"(m.conversation_id IN (SELECT id FROM conversations WHERE project = ?) "
"OR m.task_id IN (SELECT id FROM tasks WHERE project_id = ?))"
```

This means: a chunk inherits its project scope from its message, which is scoped via either `conversation_id → conversations.project` or `task_id → tasks.project_id`.

### 2.2 `chunks_vec` Table

**Location:** `switchboard/db/schema.py:591-597`

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(embedding float[1536])
```

This is a sqlite-vec virtual table for fast approximate nearest-neighbor search. It mirrors `message_chunks` using the same `id` as `rowid`.

**Insert:** Done inline during `index_message_chunks()` (`db/search.py:477-484`):
```python
if blob and cursor.lastrowid and len(blob) == 1536 * 4:
    await db.execute(
        "INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
        (cursor.lastrowid, blob),
    )
```

**Delete trigger:** (`schema.py:744-747`)
```sql
CREATE TRIGGER IF NOT EXISTS chunks_vec_delete
    AFTER DELETE ON message_chunks BEGIN
        DELETE FROM chunks_vec WHERE rowid = old.id;
    END;
```

This trigger fires on direct deletes AND on cascade deletes (when a parent message is deleted, `ON DELETE CASCADE` fires on `message_chunks`, which in turn fires this trigger to clean up `chunks_vec`).

### 2.3 Other vec0 Tables

**`messages_vec`** — mirrors `messages.embedding`, keyed by `messages.id`:
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS messages_vec USING vec0(embedding float[1536])
-- Delete trigger: AFTER DELETE ON messages → DELETE FROM messages_vec WHERE rowid = old.id
```

**`tasks_vec`** — mirrors `tasks.embedding`, keyed by `tasks.rowid`:
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS tasks_vec USING vec0(embedding float[1536])
-- Delete trigger: AFTER DELETE ON tasks → DELETE FROM tasks_vec WHERE rowid = old.rowid
```

### 2.4 Chunking Logic

**Location:** `switchboard/embeddings/chunks.py`

The `chunk_message()` function splits markdown content by `##` / `###` headers:

1. **Skip if < 500 chars** (`MIN_CHUNK_LENGTH = 500`)
2. **Skip if no markdown headers** (`^#{1,3} ` pattern)
3. **Split on headers** using `re.split(r'(?=^#{1,3} )', content, flags=re.MULTILINE)`
4. **Skip if only 1 section** (splitting didn't produce multiple chunks)
5. **Returns:** `[{"chunk_index": 0, "heading": "Section Title", "content": "full section text"}, ...]`

**Key characteristics:**
- **No overlap** between chunks — each is a clean section boundary
- **No token limit** per chunk — sections can be arbitrarily long
- **No recursive splitting** — content without markdown headers is NOT chunked
- The embedding model truncates at ~32K chars (`text[:32000]` in `service.py:138`)

### 2.5 What Triggers Embedding

Embedding happens via two paths:

#### Path 1: Inline on message creation (real-time)
**Location:** `server/handlers/common.py:15-33`

When `_handle_post_task_message` or `_handle_post_message` creates a message, it fires `_embed_message_async()` as a fire-and-forget `asyncio.create_task()`:

```python
# In _handle_post_task_message (tasks.py:425-427):
asyncio.create_task(
    _embed_message_async(result["id"], arguments["content"], arguments.get("type"))
)
```

`_embed_message_async` does two things:
1. **Embeds the full message** → stores vector in `messages.embedding` + `messages_vec`
2. **Chunks the message** → calls `db.index_message_chunks()` which chunks, embeds each chunk, and inserts into `message_chunks` + `chunks_vec`

**Skipped if:**
- Content < 50 chars (`MIN_CONTENT_LENGTH`)
- Message type is `test-result` (`SKIP_TYPES`)

#### Path 2: Background backfill (startup)
**Location:** `server/app.py:395-414` and `app.py:281-392`

On server startup, several backfill tasks run as background coroutines:

1. **`_backfill_message_chunks()`** — Finds messages >= 500 chars without any `message_chunks` entry, then calls `index_message_chunks()` for each
2. **`_backfill_vec_tables()`** — Populates `messages_vec`, `tasks_vec`, `chunks_vec` from existing BLOB embeddings (processes in batches of 1000)
3. **`_backfill_task_goals()`** — Embeds task goals that have no embedding yet
4. **`_backfill_fts_indexes()`** — Rebuilds FTS5 indexes (`messages_fts`, `tasks_fts`) using `'rebuild'` command

### 2.6 Type Weights for Relevance Scoring

**Location:** `switchboard/embeddings/service.py:49-59`

| Type | Weight |
|------|--------|
| spec | 1.5 |
| review | 1.4 |
| note | 1.2 |
| result | 1.1 |
| plan | 1.1 |
| answer | 1.0 |
| question | 0.8 |
| status | 0.5 |
| test-result | 0.3 |

Pinned messages get an additional 1.3x boost (`PINNED_BOOST`).

---

## 3. Search Handler

### 3.1 `_handle_search` — Unified Search

**Location:** `switchboard/server/handlers/search.py:80-291`

This is the single entry point for the `search` MCP tool. It searches across three entity types simultaneously:

1. **Tasks** (by goal text)
2. **Messages** (by content)
3. **Message chunks** (by chunk content)

#### Search execution flow

```
Query arrives
  ├── Embed query via OpenAI (or skip if no API key)
  ├── If embeddings available:
  │     Run 5 queries in parallel (asyncio.gather):
  │       1. search_messages_fts(query)      → BM25 keyword matches on messages
  │       2. search_messages_semantic(vec)    → cosine similarity on messages
  │       3. search_tasks_fts(query)          → BM25 keyword matches on task goals
  │       4. search_tasks_semantic(vec)       → cosine similarity on task goals
  │       5. search_message_chunks(vec)       → cosine similarity on chunks
  │   Else (FTS-only fallback):
  │       1. search_messages_fts(query)
  │       2. search_tasks_fts(query)
  └── Merge, score, deduplicate, return top N
```

### 3.2 Hybrid Scoring Pipeline

For each candidate, the final score is computed as:

```
final_score = base_score × type_boost × pinned_boost × dual_match_boost × recency_mult
```

**Base score (hybrid FTS + vec):**
- Messages: `0.4 × fts_norm + 0.6 × vec_sim` (semantic weighted higher)
- Tasks: `0.6 × fts_norm + 0.4 × vec_sim` (keyword precision weighted higher)
- Chunks: `vec_sim` only (no FTS for chunks)

FTS BM25 scores are normalized to 0-1 by dividing by the max score in the result set.

**Boosts:**
- **Type boost:** Messages get weighted by type (spec=1.5x, review=1.4x, etc.)
- **Pinned boost:** Pinned messages get 1.3x
- **Dual match boost:** 1.3x if the entity appears in BOTH FTS and vec results
- **Recency decay:** Linear from 1.0 (today) to 0.3 (90 days old). Formula: `1.0 - (min(days_old, 90) / 90 × 0.7)`

### 3.3 Result Structure

Each search result is a flat dict:

```python
{
    "type": "task" | "task_message" | "conversation_message" | "chunk",
    "entity_id": str,          # task_id for tasks, message_id (as str) for messages/chunks
    "task_id": str | None,     # Set for task_messages and chunks with task context
    "conversation_id": str | None,  # Set for conversation_messages
    "title": str | None,       # Message title, task goal, or chunk heading
    "snippet": str,            # Plain text (markdown stripped), max 200 chars
    "relevance_score": float,  # Final weighted score (0-1+ range)
    "author": str | None,
    "message_type": str | None,  # Original message type (spec, result, etc.)
    "created_at": str | None,
    "status": str | None,      # Only for task results
}
```

**How `entity_id` is set:**
- For tasks: `entity_id = task_id` (the TEXT primary key like `"mcp-switchboard/my-task"`)
- For messages: `entity_id = str(message_id)` (the INTEGER autoincrement id, cast to string)
- For chunks: `entity_id = str(hit["message_id"])` (the parent message's id)

**How `type` is determined:**
- `"task"` — always for task goal matches
- `"task_message"` — message with a non-null `task_id`
- `"conversation_message"` — message with a null `task_id` (conversation-only)
- `"chunk"` — chunk hit (always from `search_message_chunks`)

**Deduplication:** When a message has both a whole-message hit and a chunk hit, the chunk hit takes precedence (message is excluded from `msg_candidates`). Final dedup keeps the highest-scoring entry per `entity_id`.

### 3.4 Project ID Scoping

All search functions accept an optional `project_id` parameter. The scoping SQL is consistent across messages, chunks, and tasks:

**Messages and chunks** (both use the same pattern in `db/search.py`):
```sql
(m.conversation_id IN (SELECT id FROM conversations WHERE project = ?)
 OR m.task_id IN (SELECT id FROM tasks WHERE project_id = ?))
```

This covers both paths: messages posted to conversations (scoped via `conversations.project`) and messages posted to tasks (scoped via `tasks.project_id`).

**Tasks:**
```sql
t.project_id = ?
```

### 3.5 Chunk Search Details

**Location:** `db/search.py:488-635`

`search_message_chunks()` returns the top N chunk hits plus **adjacent context** (chunks at index ±1):

```python
for hit in top:
    adj_rows = await db.execute_fetchall(
        """SELECT chunk_index, heading, content FROM message_chunks
           WHERE message_id = ? AND chunk_index IN (?, ?)""",
        (hit["message_id"], hit["chunk_index"] - 1, hit["chunk_index"] + 1),
    )
    hit["context_chunks"] = [...]
```

Sentinel rows (`chunk_index = -1`) are filtered out in queries via `mc.chunk_index >= 0`.

---

## 4. Design: File Content in Search

### 4.1 Requirements Recap

1. **Zero data duplication** — file content stays on disk; only chunks + vectors are created
2. **Cascade delete** — removing a file cleans up its chunks and vec entries
3. **Search results include `file_id`** — so callers can call `get_file(file_id)` to read the full document
4. **Backfill** — existing text files get indexed
5. **Recency decay** — file chunks get `created_at` and existing decay applies

### 4.2 Schema Changes

#### New table: `file_chunks`

```sql
CREATE TABLE IF NOT EXISTS file_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    heading TEXT,
    content TEXT NOT NULL,
    embedding BLOB,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_file_chunks_file_id ON file_chunks(file_id);
```

**Why a separate table (not reusing `message_chunks`):**
- `message_chunks.message_id` is `INTEGER NOT NULL` — files have TEXT UUIDs as IDs
- The join path for project scoping is different: `file_chunks → files → task_id/project_id` vs `message_chunks → messages → conversation_id/task_id`
- Keeps the existing message chunking pipeline untouched — no risk of breaking 900+ tests
- Cleaner cascade: `ON DELETE CASCADE` from `files` directly cleans up file chunks

#### New vec0 table: `file_chunks_vec`

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS file_chunks_vec USING vec0(embedding float[1536])
```

#### New trigger: `file_chunks_vec_delete`

```sql
CREATE TRIGGER IF NOT EXISTS file_chunks_vec_delete
    AFTER DELETE ON file_chunks BEGIN
        DELETE FROM file_chunks_vec WHERE rowid = old.id;
    END;
```

**Cascade chain:**
```
DELETE FROM files WHERE id = ?
  → ON DELETE CASCADE → DELETE FROM file_chunks WHERE file_id = ?
    → TRIGGER file_chunks_vec_delete → DELETE FROM file_chunks_vec WHERE rowid = old.id
```

This mirrors the existing `messages → message_chunks → chunks_vec` cascade exactly.

### 4.3 Code Changes

#### 4.3.1 Schema: `switchboard/db/schema.py`

Add the `file_chunks` table creation after the existing `message_chunks` block (around line 689):

```python
CREATE TABLE IF NOT EXISTS file_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    heading TEXT,
    content TEXT NOT NULL,
    embedding BLOB,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_file_chunks_file_id ON file_chunks(file_id);
```

Add `file_chunks_vec` creation alongside existing vec0 tables (around line 591):

```python
if "file_chunks_vec" not in vec_table_names:
    try:
        await conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS file_chunks_vec USING vec0(embedding float[1536])"
        )
    except Exception:
        pass
```

Add the delete trigger alongside existing vec0 triggers (around line 744):

```python
CREATE TRIGGER IF NOT EXISTS file_chunks_vec_delete
    AFTER DELETE ON file_chunks BEGIN
        DELETE FROM file_chunks_vec WHERE rowid = old.id;
    END;
```

Update the vec table existence check query to include `file_chunks_vec`.

#### 4.3.2 Indexing: `switchboard/db/search.py`

Add a new function `index_file_chunks()` modeled on `index_message_chunks()`:

```python
async def index_file_chunks(file_id: str, content: str, filename: str) -> None:
    """Chunk a text file and embed each chunk. Idempotent — deletes existing chunks first."""
    from switchboard.embeddings.chunks import chunk_message
    from switchboard.embeddings.service import get_embedding_service, encode_vector

    chunks = chunk_message(content)

    async with get_db() as db:
        await db.execute("DELETE FROM file_chunks WHERE file_id = ?", (file_id,))

        if not chunks:
            # For files without markdown headers, treat entire content as one chunk
            # (unlike messages, ALL text files should be searchable)
            chunks = [{"chunk_index": 0, "heading": filename, "content": content}]

        service = get_embedding_service()
        for chunk in chunks:
            vector = await service.embed_safe(chunk["content"])
            blob = encode_vector(vector) if vector else None
            cursor = await db.execute(
                """INSERT INTO file_chunks (file_id, chunk_index, heading, content, embedding)
                   VALUES (?, ?, ?, ?, ?)""",
                (file_id, chunk["chunk_index"], chunk["heading"], chunk["content"], blob),
            )
            if blob and cursor.lastrowid and len(blob) == 1536 * 4:
                try:
                    await db.execute(
                        "INSERT OR REPLACE INTO file_chunks_vec(rowid, embedding) VALUES (?, ?)",
                        (cursor.lastrowid, blob),
                    )
                except Exception as e:
                    log.warning("file_chunks_vec insert failed for chunk rowid %d: %s", cursor.lastrowid, e)
        await db.commit()
```

**Key difference from message chunking:** Files without markdown headers should still be indexed as a single chunk (the entire file content). Messages skip chunking in this case, but files are specifically being added to search — skipping them defeats the purpose.

Add a new function `search_file_chunks()`:

```python
async def search_file_chunks(
    query_vector: list[float],
    project_id: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Search file chunks by vector similarity."""
    from switchboard.embeddings.service import encode_vector

    if len(query_vector) != _VEC_DIM:
        return []  # No fallback needed for files

    blob = encode_vector(query_vector)
    oversample = limit * 10

    try:
        async with get_db() as db:
            vec_rows = await db.execute_fetchall(
                "SELECT rowid, distance FROM file_chunks_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (blob, oversample),
            )
    except Exception:
        return []

    if not vec_rows:
        return []

    rowids = [r["rowid"] for r in vec_rows]
    distance_map = {r["rowid"]: r["distance"] for r in vec_rows}

    id_placeholders = ",".join("?" * len(rowids))
    conditions = [f"fc.id IN ({id_placeholders})"]
    params = list(rowids)

    if project_id:
        conditions.append(
            "(f.task_id IN (SELECT id FROM tasks WHERE project_id = ?) "
            "OR f.project_id = ?)"
        )
        params.extend([project_id, project_id])

    where = " AND ".join(conditions)
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"""SELECT fc.id, fc.file_id, fc.chunk_index, fc.heading, fc.content,
                       f.filename, f.task_id, f.project_id, f.created_at
                FROM file_chunks fc
                JOIN files f ON f.id = fc.file_id
                WHERE {where}""",
            params,
        )

    results = []
    for row in rows:
        distance = distance_map[row["id"]]
        similarity = max(0.0, 1.0 - (distance / 2.0))
        results.append({
            "chunk_id": row["id"],
            "file_id": row["file_id"],       # <-- THE KEY: UUID for get_file()
            "filename": row["filename"],
            "chunk_index": row["chunk_index"],
            "chunk_heading": row["heading"],
            "chunk_content": row["content"],
            "task_id": row["task_id"],
            "project_id": row["project_id"],
            "created_at": row["created_at"],
            "similarity": similarity,
        })

    results.sort(key=lambda r: r["similarity"], reverse=True)
    return results[:limit]
```

#### 4.3.3 Search handler: `switchboard/server/handlers/search.py`

Modify `_handle_search()` to include file chunks:

1. **Add `search_file_chunks` to the parallel `asyncio.gather`** (alongside the existing 5 queries):
   ```python
   file_chunk_hits = await db.search_file_chunks(query_vector, project_id=project_id, limit=limit)
   ```

2. **Build file chunk candidates** (new block after chunk_candidates):
   ```python
   file_chunk_candidates = []
   for hit in file_chunk_hits:
       base = hit["similarity"]
       rec_mult = _recency_mult(hit.get("created_at"), now)
       # Files get a neutral type weight (1.0) — no type/pinned boost
       final_score = base * rec_mult

       file_chunk_candidates.append({
           "type": "file",
           "entity_id": hit["file_id"],      # UUID — caller uses get_file(entity_id)
           "task_id": hit.get("task_id"),
           "project_id": hit.get("project_id"),
           "conversation_id": None,
           "title": hit.get("filename"),
           "snippet": _make_search_snippet(hit.get("chunk_content") or ""),
           "relevance_score": round(final_score, 4),
           "author": None,
           "message_type": None,
           "created_at": hit.get("created_at"),
           "file_id": hit["file_id"],         # Explicit file_id for convenience
       })
   ```

3. **Merge into `all_candidates`:**
   ```python
   all_candidates = task_candidates + msg_candidates + chunk_candidates + file_chunk_candidates
   ```

**How `file_id` surfaces to callers:**
- `entity_id` is set to the file's UUID (`hit["file_id"]`)
- An explicit `file_id` field is also included for clarity
- `type` is `"file"` so the caller knows to call `get_file(entity_id)` to retrieve content
- This is unlike message results where `entity_id` is the message id — for files, the UUID directly maps to `get_file(id=entity_id)`

#### 4.3.4 Trigger indexing on file upload: `switchboard/server/handlers/files_handler.py`

In `_handle_add_task_file()` and `_handle_add_project_file()`, after `db.create_file()`, fire async indexing for text files:

```python
# After db.create_file() returns:
if _is_readable(filename):
    asyncio.create_task(_index_file_async(file_id, str(dest)))

async def _index_file_async(file_id: str, stored_path: str) -> None:
    """Fire-and-forget: read file content and index for search."""
    try:
        content = Path(stored_path).read_text(errors="replace")
        if len(content) >= 50:  # MIN_CONTENT_LENGTH
            await db.index_file_chunks(file_id, content, Path(stored_path).name)
    except Exception:
        pass  # Best-effort, never block
```

#### 4.3.5 Backfill: `switchboard/server/app.py`

Add a new startup backfill function:

```python
async def _backfill_file_chunks() -> None:
    """Background task: index existing text files that haven't been chunked yet."""
    from switchboard.server.handlers.files_handler import READABLE_EXTENSIONS
    total = 0
    try:
        async with get_db() as conn:
            rows = await conn.execute_fetchall(
                """SELECT f.id, f.filename, f.stored_path FROM files f
                   WHERE NOT EXISTS (SELECT 1 FROM file_chunks fc WHERE fc.file_id = f.id)
                   ORDER BY f.created_at ASC"""
            )
        for row in rows:
            ext = row["filename"].rsplit(".", 1)[-1].lower() if "." in row["filename"] else ""
            if ext not in READABLE_EXTENSIONS:
                continue
            try:
                content = Path(row["stored_path"]).read_text(errors="replace")
                if len(content) >= 50:
                    await db.index_file_chunks(row["id"], content, row["filename"])
                    total += 1
            except Exception as e:
                log.warning("File chunk backfill failed for %s: %s", row["id"], e)
        if total > 0:
            log.info("File chunk backfill complete: %d files processed", total)
    except Exception as e:
        log.error("File chunk backfill aborted: %s", e)
```

Register in lifespan startup:
```python
asyncio.create_task(_backfill_file_chunks())
```

Also update `_backfill_vec_tables()` to include `file_chunks_vec`:

```python
# New batch loop for file_chunks → file_chunks_vec
file_chunk_count = 0
offset = 0
while True:
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT id, embedding FROM file_chunks "
            "WHERE embedding IS NOT NULL LIMIT ? OFFSET ?",
            (_BATCH_SIZE, offset),
        )
        if not rows:
            break
        for row in rows:
            if len(row["embedding"]) != _expected_blob_len:
                continue
            try:
                await conn.execute(
                    "INSERT OR REPLACE INTO file_chunks_vec(rowid, embedding) VALUES (?, ?)",
                    (row["id"], row["embedding"]),
                )
                file_chunk_count += 1
            except Exception:
                pass
        await conn.commit()
    offset += _BATCH_SIZE
```

Add orphan reconciliation for `file_chunks_vec`:
```python
await conn.execute(
    "DELETE FROM file_chunks_vec WHERE rowid NOT IN "
    "(SELECT id FROM file_chunks WHERE embedding IS NOT NULL)"
)
```

#### 4.3.6 DB exports: `switchboard/db/__init__.py`

Export the new functions:
```python
from switchboard.db.search import index_file_chunks, search_file_chunks
```

#### 4.3.7 Delete cleanup (already handled)

No additional work needed:
- `db.delete_file()` → `DELETE FROM files WHERE id = ?` → `ON DELETE CASCADE` on `file_chunks` → trigger `file_chunks_vec_delete` on `file_chunks_vec`
- Dashboard `_handle_delete_file()` already calls `db.delete_file()` after removing from disk
- `delete_project()` deletes files in bulk — the cascade handles chunks automatically

#### 4.3.8 Check vec availability: `switchboard/db/search.py`

Update `_check_vec_tables()` to also check `file_chunks_vec`:
```python
await db.execute_fetchall("SELECT count(*) FROM file_chunks_vec LIMIT 1")
```

### 4.4 Files NOT Modified

- `switchboard/embeddings/chunks.py` — reuse `chunk_message()` as-is for markdown files
- `switchboard/embeddings/service.py` — no changes needed
- `switchboard/server/tools.py` — no new MCP tools needed; `search` tool just returns more result types
- `switchboard/server/dispatch.py` — no routing changes

### 4.5 Testing Approach

1. **Unit test `index_file_chunks()`** — create a file, index it, verify `file_chunks` and `file_chunks_vec` rows
2. **Unit test `search_file_chunks()`** — index a file, search by vector, verify results include `file_id`
3. **Test cascade delete** — create file + chunks, delete file, verify chunks and vec entries are gone
4. **Test `_handle_search` with file results** — verify file chunks appear in unified search with `type: "file"` and `entity_id` = file UUID
5. **Test project_id scoping** — file attached to task in project A should not appear in project B search
6. **Test backfill** — create files without chunks, run backfill, verify chunks created

### 4.6 Step-by-Step Implementation Order

1. **Schema** — Add `file_chunks` table, `file_chunks_vec` virtual table, and delete trigger to `schema.py`
2. **DB functions** — Add `index_file_chunks()` and `search_file_chunks()` to `db/search.py`
3. **DB exports** — Export new functions from `db/__init__.py`
4. **File handler integration** — Add async indexing call in `_handle_add_task_file()` and `_handle_add_project_file()`
5. **Search handler** — Add file chunk search to `_handle_search()` in `server/handlers/search.py`
6. **Backfill** — Add `_backfill_file_chunks()` to `app.py`, update `_backfill_vec_tables()` and `_check_vec_tables()`
7. **Tests** — Write tests covering indexing, search, cascade, scoping, and backfill
8. **Manual verification** — Upload a markdown file via worker, search for its content, verify `file_id` in results
