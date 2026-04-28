# Living Docs — Implementation Plan

**Project:** mcp-switchboard (package `ouvrage/`)
**Source of truth:** task spec for `mcp-switchboard/living-docs-plan` and the pinned spec on conversation `living-docs` (the names below — `reference_doc_configs`, `reference_doc_versions`, `set_reference_doc_config`, `regenerate_reference_docs`, `add_reference_doc_version` — match the canonical spec).
**Author:** cc-worker, 2026-04-28
**Scope:** analysis only. No code changes outside this file.

> Note on path naming: the task spec sometimes uses `switchboard/...`. The package was renamed to `ouvrage/` in this repo; every code reference below is to the actual current path (`ouvrage/db/schema.py`, etc.).

---

## 1. Open question resolutions

### 1.1 Merged-PRs-per-project queryability

**Verdict:** queryable today by `pr_status='merged'` and `project_id`, **but** there is no reliable per-row "merged at" timestamp. A small prerequisite (`tasks.merged_at TIMESTAMP`) is recommended.

**Code-grounded findings:**

- PR URLs are not stored on the `tasks` row. They live in `task_artifacts` with `type='pr_url'`:
  - `ouvrage/git/operations.py:338` — `await db.add_artifact(task_id, "pr_url", pr_url)` after PR creation.
  - `ouvrage/db/tasks.py:281` — `get_tasks_with_open_prs()` joins through `task_artifacts` to read the URL.
- `pr_status` is the canonical merged signal:
  - `'merged'` set by **auto-merge** at `ouvrage/git/operations.py:459` — `await db.update_task(task_id, status="merged", pushed_at=db.now_iso(), pr_status="merged")`. Here `pushed_at` *is* the merge timestamp.
  - `'merged'` also set by the **PR sweep** at `ouvrage/dispatch/pr_sweep.py:108` — `await db.update_task(task["id"], pr_status=status)` only. **`pushed_at` is *not* updated** in the sweep path. So `pushed_at` is the merge time only when auto-merge handled it; otherwise it's the original push time.
  - `_handle_pr_merged` posts a `"PR merged"` status message and may transition `status='merged'` (`ouvrage/dispatch/pr_sweep.py:59`).
- `tasks.updated_at` does change when `pr_status` is updated, but it also changes for unrelated updates, so it's not a clean merge timestamp.
- The `pr_status` and `pushed_at` columns are part of `TASK_MUTABLE_FIELDS` (`ouvrage/config/constants.py:53`) — updates go through the existing `db.update_task(...)` path.

**Recommendation: prerequisite work — add `tasks.merged_at`.**

1. Migration in `ouvrage/db/schema.py`: `ALTER TABLE tasks ADD COLUMN merged_at TIMESTAMP`. Add `merged_at` to `TASK_MUTABLE_FIELDS` (`ouvrage/config/constants.py`).
2. Set it at both merge call sites:
   - `ouvrage/git/operations.py:459` — include `merged_at=db.now_iso()` in the same `update_task` call.
   - `ouvrage/dispatch/pr_sweep.py:108` — when the new status is `'merged'`, include `merged_at=db.now_iso()`.
3. Backfill: best-effort `UPDATE tasks SET merged_at = pushed_at WHERE pr_status='merged' AND merged_at IS NULL` during migration. For sweep-merged rows where `pushed_at` is the original push, this is approximate but better than NULL — the regen flow only needs ordinal recency, not minute-precision.
4. Add a query helper to `ouvrage/db/tasks.py`:

   ```python
   async def list_merged_tasks_since(project_id: str, since_iso: str | None) -> list[dict]:
       """Tasks whose PR has merged in this project since `since_iso` (or all if None)."""
       async with get_db() as db:
           sql = """
               SELECT t.id, t.goal, t.branch, t.merged_at, t.component_id,
                      (SELECT ref FROM task_artifacts WHERE task_id = t.id AND type='pr_url' LIMIT 1) AS pr_url
                 FROM tasks t
                WHERE t.project_id = ?
                  AND t.pr_status = 'merged'
                  AND (? IS NULL OR t.merged_at > ?)
                ORDER BY t.merged_at ASC
           """
           rows = await db.execute_fetchall(sql, (project_id, since_iso, since_iso))
           return [dict(r) for r in rows]
   ```

   This is the function the regen scheduler and `regenerate_reference_docs` call to pull "merged since last regen."

**Fallback if reviewers descope `merged_at`:** use `updated_at` plus `pr_status='merged'` and accept the approximation. Listed under §10 risks.

### 1.2 Files table role flag

**Verdict: add an explicit `role TEXT NOT NULL DEFAULT 'upload'` column on `files`.** Recommend (a) over inferring via FK from `reference_doc_versions.file_id`.

**Code-grounded findings:**

- Current schema (`ouvrage/db/schema.py:259`) has no role/kind/type column on `files`.
- `delete_file` (`ouvrage/db/files.py:82`) is a bare `DELETE FROM files WHERE id = ?` — no FK protection, no validation, returns bool.
- `_handle_get_file` and `_handle_list_files` (`ouvrage/server/handlers/files_handler.py:41`, `:252`) return all columns; the dashboard would benefit from a stable role flag to render reference docs differently from uploads.
- The unified search handler (`ouvrage/server/handlers/search.py`) doesn't read files today; when it does (§7), `WHERE files.role = 'reference_doc'` is a much cheaper filter than a JOIN through `reference_doc_versions`.

**Reasoning for the column over FK inference:**

| Concern | Column-based | FK-inference |
|---|---|---|
| Delete protection | Single check in `delete_file()`: `WHERE id = ? AND role != 'reference_doc'` — single row touch | Subquery against `reference_doc_versions` and `reference_doc_configs` per delete |
| `scope=docs` search filter | `WHERE files.role = 'reference_doc'` | JOIN through versions and configs |
| Dashboard rendering | Direct field on file rows | N+1 lookups or extra JOIN |
| Future "doc archive" semantics | A doc that's been deleted from configs but kept as a version is still `role='reference_doc'` | Inference would mark it as upload-equivalent, losing its identity |
| Migration cost | One `ALTER TABLE` + `DEFAULT 'upload'` | None (good) |

The migration cost is trivial. Choose explicit role.

**Delete protection enforcement:**

Add a guard in `ouvrage/db/files.py:delete_file`:

```python
async def delete_file(id: str) -> bool:
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT role FROM files WHERE id = ?", (id,))
        if rows and rows[0]["role"] == "reference_doc":
            raise ValueError(
                f"File {id} is a reference doc version and cannot be deleted directly. "
                "Use delete_reference_doc_config(...) which cascades to versions."
            )
        cursor = await conn.execute("DELETE FROM files WHERE id = ?", (id,))
        await conn.commit()
        return cursor.rowcount > 0
```

`ValueError` is the existing convention used by file handlers (e.g., `ouvrage/server/handlers/files_handler.py:104`, `:107`, `:121`) and is wrapped by the MCP layer into a tool error. No new error type is required.

The complementary cascade — deleting reference docs through their config — lives on the new `LivingDocsService.delete_config()` (§3) and uses `ON DELETE CASCADE` on `reference_doc_versions.config_id` plus an explicit `DELETE FROM files WHERE id IN (SELECT file_id FROM reference_doc_versions WHERE config_id = ?)` *before* the cascade fires (we want the file rows gone too). To make that delete safe even though the role guard would block it, expose an internal `db.delete_reference_doc_files(config_id)` that bypasses the guard.

### 1.3 Embedding write path for files

**Verdict:** the chunker handles arbitrary `.md` content given §5's system prompt enforces headed sections. Embedding hook lives on the service layer (`LivingDocsService.add_version`). Storage is two new tables (`file_chunks`, `file_chunks_vec`) plus `files_vec` for whole-file fallback. `scope=docs` filtering joins through `reference_doc_configs.current_version_id`.

**Code-grounded findings:**

- Chunker contract (`ouvrage/embeddings/chunks.py:8`): returns `None` when content < 500 chars, no `## ` / `### ` headers, or only one section. Otherwise returns `[{chunk_index, heading, content}, ...]`. Sections are split on `## ` / `### ` only — `# ` and `#### ` are not split markers.
- The existing chunk-write code path is `ouvrage/db/search.py:443` — `index_message_chunks(message_id, content)`:
  - Idempotent (deletes prior chunks first).
  - Inserts a sentinel row `chunk_index=-1` if chunking returned None — keeps the "needs chunking" predicate false. We'll mirror this for files.
  - Embeds each chunk via `embed_safe`, packs to float32, INSERT into `message_chunks`, also `INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (cursor.lastrowid, blob)` when `len(blob) == 1536*4`. Same shape for `file_chunks_vec`.
- vec0 declarations live in `ouvrage/db/schema.py:638–660` inside a `try` because sqlite-vec may not be loaded; `VEC_AVAILABLE` flag (`ouvrage/db/search.py:18`) gates queries. New file vec0 tables follow the same try/except + flag check pattern.
- Today files are NOT embedded. Adding files is therefore additive; no triggers in `vec0_delete` need rewriting. We only add new triggers for the file path.
- Files are embedded **on `add_version`** (after `db.create_file(role='reference_doc')`), via `asyncio.create_task` to avoid blocking the worker tool call. This mirrors the deferred-write pattern of `_backfill_*` loops in `ouvrage/server/app.py:485–499`. A startup backfill task `_backfill_file_chunks` covers files added during a window when the embedding service was unavailable.
- The chunker requires `## ` / `### ` headers and ≥500 chars to return chunks. The §5 system prompt mandates `## ` sections (Overview / Architecture / Data Flow / Interfaces / Risks / Recent Changes / Open Questions) so chunking will always succeed for properly generated reference docs. For shorter or atypical docs, the sentinel + whole-file vec entry keeps them searchable.

**Storage layout:**

```sql
-- whole-file embedding (always present when len(content) >= MIN_CONTENT_LENGTH)
CREATE TABLE files_embeddings (
    file_id   TEXT PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    embedding BLOB,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE files_vec USING vec0(embedding float[1536]);
-- rowid in files_vec = files.rowid (parallel to tasks_vec)

-- chunk-level embeddings for finer-grained semantic hits within a long doc
CREATE TABLE file_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     TEXT    NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    heading     TEXT,
    content     TEXT    NOT NULL,
    embedding   BLOB,
    created_at  TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX idx_file_chunks_file ON file_chunks(file_id);
CREATE VIRTUAL TABLE file_chunks_vec USING vec0(embedding float[1536]);
```

Triggers (mirror `chunks_vec_delete` at schema.py:808):

```sql
CREATE TRIGGER files_vec_delete AFTER DELETE ON files BEGIN
    DELETE FROM files_vec WHERE rowid = old.rowid;
END;
CREATE TRIGGER file_chunks_vec_delete AFTER DELETE ON file_chunks BEGIN
    DELETE FROM file_chunks_vec WHERE rowid = old.id;
END;
```

`ON DELETE CASCADE` from `files → file_chunks → file_chunks_vec` means a single `DELETE FROM files WHERE id = ?` cleans everything up.

**Embedding hook placement:**

```python
# ouvrage/services/living_docs.py
class LivingDocsService:
    async def add_version(self, config_id: str, file_path: str, citations: list[dict]) -> dict:
        file_record = await self._upload_file(file_path, role='reference_doc')
        version = await db.create_reference_doc_version(
            config_id=config_id, file_id=file_record["id"], citations_json=citations
        )
        # Fire-and-forget embedding indexing — same pattern as _backfill_* loops.
        asyncio.create_task(_index_doc_file(file_record["id"]))
        return version
```

`_index_doc_file` (in `ouvrage/db/search.py`, mirroring `index_message_chunks`):

```python
async def index_doc_file(file_id: str) -> None:
    record = await db.get_file(file_id)
    if not record or record["role"] != "reference_doc":
        return
    content = Path(record["stored_path"]).read_text(encoding="utf-8", errors="replace")

    service = get_embedding_service()
    whole = await service.embed_safe(content[:32000])  # service truncates
    if whole:
        blob = encode_vector(whole)
        await db.set_file_embedding(file_id, blob)  # writes files_embeddings + files_vec

    chunks = chunk_message(content)  # reuse existing chunker
    async with get_db() as conn:
        await conn.execute("DELETE FROM file_chunks WHERE file_id = ?", (file_id,))
        if not chunks:
            await conn.execute(
                """INSERT INTO file_chunks (file_id, chunk_index, heading, content, embedding)
                   VALUES (?, -1, NULL, '', NULL)""", (file_id,))
            await conn.commit()
            return
        for ch in chunks:
            vec = await service.embed_safe(ch["content"])
            blob = encode_vector(vec) if vec else None
            cur = await conn.execute(
                """INSERT INTO file_chunks (file_id, chunk_index, heading, content, embedding)
                   VALUES (?, ?, ?, ?, ?)""",
                (file_id, ch["chunk_index"], ch["heading"], ch["content"], blob))
            if blob and cur.lastrowid and len(blob) == 1536 * 4:
                try:
                    await conn.execute(
                        "INSERT OR REPLACE INTO file_chunks_vec(rowid, embedding) VALUES (?, ?)",
                        (cur.lastrowid, blob))
                except Exception as e:
                    log.warning("file_chunks_vec insert failed for file %s: %s", file_id, e)
        await conn.commit()
```

**`scope=docs` filter — current versions only:**

```sql
-- File-id whitelist for "current versions only"
SELECT v.file_id
  FROM reference_doc_configs c
  JOIN reference_doc_versions v ON v.id = c.current_version_id
 WHERE c.project_id = ?
```

In `search_files_semantic` (new function in `ouvrage/db/search.py` mirroring `search_messages_semantic`), the filter becomes:

```sql
SELECT f.id, fc.chunk_index, fc.heading, fc.content, ...
  FROM files_vec fv
  JOIN files       f  ON f.rowid = fv.rowid
  -- (or join through file_chunks_vec for chunk-level hits)
 WHERE fv.embedding MATCH ? AND k = ?
   AND f.role = 'reference_doc'
   AND f.id IN (
       SELECT v.file_id FROM reference_doc_configs c
         JOIN reference_doc_versions v ON v.id = c.current_version_id
        WHERE c.project_id = ?
   )
   -- when project_id is None, drop the inner restriction or scope to all current versions
 ORDER BY fv.distance
```

When the caller wants prior versions included (e.g., `scope='docs+history'`), the `c.current_version_id` restriction becomes `f.id IN (SELECT file_id FROM reference_doc_versions WHERE config_id IN (SELECT id FROM reference_doc_configs WHERE project_id = ?))` and the score multiplier is 0.5× (§4).

### 1.4 Initial weight values

These values plug into the unified search scorer (`ouvrage/server/handlers/search.py:96` `_handle_search`) by extending `_VALID_ENTITY_TYPES` (in `ouvrage/db/search_weights.py:6`) and adding a per-source default-weight map alongside the existing `_TYPE_BOOST`.

Recommended initial values:

| Source | Weight | Reasoning |
|---|---|---|
| **Project reference doc current versions** | **1.6** | These are the curated, regenerated, citation-grounded summary of the codebase. They should outrank any single message. 1.6 is calibrated to slightly exceed a `spec` message (1.5) — a current reference doc is a refined, agent-validated form of the same intent. |
| **Task `.md` artifacts** | **1.0** | Baseline. These are useful but raw — typically a one-shot task plan or report. They should be findable but not dominate over a curated reference doc. |
| **Prior reference doc versions** | **0.5** | Kept indexed so users can grep "what did this doc say last quarter?" but de-prioritized so they never displace the current version in default search. Same multiplier as the search recency floor (`RECENCY_FLOOR=0.3` plus a small boost to remain visible). |
| **Prose messages (existing)** | **unchanged** | Already calibrated by `_TYPE_BOOST` (`ouvrage/server/handlers/search.py:13`): spec=1.5, review=1.4, note=1.2, result=1.1, plan=1.1, question=0.8, status=0.5, test-result=0.3. Pinned messages get an additional ×1.3. No reason to disturb these. |

These are *defaults* — the per-entity manual weight system in `search_weights` (`ouvrage/db/search_weights.py:22`) overrides them. After the first regen wave we'll likely tune by observing the search-result mix; the values are deliberately conservative.

The new `entity_type` values to add to `_VALID_ENTITY_TYPES`:

```python
_VALID_ENTITY_TYPES = {"task", "message", "chunk", "doc_version", "doc_chunk"}
```

`doc_version` keys to `files.id`; `doc_chunk` keys to `file_chunks.id`. The default-weight map is read by `_handle_search` after deciding whether the file is a current version (multiplied by 1.6) or a prior version (multiplied by 0.5). Manual weights stack on top.

**Tunability acknowledgment:** these are starting values. After the first regen wave the dashboard should expose a small "weight tuning" view (already on the roadmap) so reviewers can A/B the docs-vs-messages mix on real queries.

---

## 2. Schema details

All migrations append to the existing dynamic-migration block in `ouvrage/db/schema.py` (the same block that runs `ALTER TABLE tasks ADD COLUMN ...` on every startup). They are idempotent and safe on a populated DB.

### 2.1 New tables

```sql
-- One config per (project, doc_name). The pre-curated brief drives regen prompts.
CREATE TABLE IF NOT EXISTS reference_doc_configs (
    id                  TEXT PRIMARY KEY,                    -- e.g. uuid4
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name                TEXT NOT NULL,                       -- short slug, e.g. "auth-overview"
    title               TEXT NOT NULL,                       -- human display, e.g. "Auth Overview"
    brief               TEXT NOT NULL,                       -- curated input shown in regen prompt
    audience            TEXT,                                -- optional voice hint
    current_version_id  INTEGER REFERENCES reference_doc_versions(id),
    last_regen_at       TIMESTAMP,                           -- when a regen task last completed
    last_regen_task_id  TEXT REFERENCES tasks(id),           -- last regen task, for traceability
    paused              BOOLEAN NOT NULL DEFAULT 0,           -- suppress automatic regen
    created_by          INTEGER REFERENCES users(id),
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL,
    UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_refdoc_configs_project ON reference_doc_configs(project_id);

-- Append-only versions. Each version is a single file in the files table (role='reference_doc').
CREATE TABLE IF NOT EXISTS reference_doc_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id       TEXT    NOT NULL REFERENCES reference_doc_configs(id) ON DELETE CASCADE,
    file_id         TEXT    NOT NULL REFERENCES files(id),    -- the .md file row (role='reference_doc')
    task_id         TEXT             REFERENCES tasks(id),    -- regen task that produced this version (NULL for bootstrap)
    citations_json  TEXT,                                     -- JSON list of {kind, ref, sha?, message_id?, task_id?}
    note            TEXT,                                     -- short summary of what changed (from regen task)
    unchanged       BOOLEAN NOT NULL DEFAULT 0,                -- agent declared no material change
    created_at      TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refdoc_versions_config ON reference_doc_versions(config_id, created_at);
CREATE INDEX IF NOT EXISTS idx_refdoc_versions_file ON reference_doc_versions(file_id);

-- File embedding helpers (parallel to messages_*/tasks_*/chunks_*)
CREATE TABLE IF NOT EXISTS files_embeddings (
    file_id   TEXT PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    embedding BLOB,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS file_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     TEXT    NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    heading     TEXT,
    content     TEXT    NOT NULL,
    embedding   BLOB,
    created_at  TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_file_chunks_file ON file_chunks(file_id);

-- vec0 virtual tables (created in try/except — no-op if sqlite-vec not loaded)
CREATE VIRTUAL TABLE IF NOT EXISTS files_vec        USING vec0(embedding float[1536]);
CREATE VIRTUAL TABLE IF NOT EXISTS file_chunks_vec  USING vec0(embedding float[1536]);
```

### 2.2 Column additions

```sql
-- files: explicit role
ALTER TABLE files ADD COLUMN role TEXT NOT NULL DEFAULT 'upload';
-- existing rows become 'upload' implicitly. Reference docs are inserted with role='reference_doc'.
CREATE INDEX IF NOT EXISTS idx_files_role ON files(role) WHERE role = 'reference_doc';

-- tasks: merge timestamp (prerequisite for §1.1)
ALTER TABLE tasks ADD COLUMN merged_at TIMESTAMP;
-- Best-effort backfill from pushed_at where pr_status is already merged.
UPDATE tasks SET merged_at = pushed_at
 WHERE pr_status = 'merged' AND merged_at IS NULL AND pushed_at IS NOT NULL;

-- projects: living-docs config blob (JSON, parsed by db/projects.py decoder)
ALTER TABLE projects ADD COLUMN living_docs_config TEXT;
-- shape: {"min_merges_to_regen": 5, "max_age_days": 14, "regen_model": "opus", "paused": false}
-- defaults applied at read time when null
```

`TASK_MUTABLE_FIELDS` in `ouvrage/config/constants.py:39` gets `"merged_at"` added. `_decode_project()` in `ouvrage/db/projects.py:67` gets `living_docs_config` JSON-decoded alongside `env_overrides`.

### 2.3 vec0 delete triggers

Mirroring `messages_vec_delete`/`chunks_vec_delete` at `ouvrage/db/schema.py:797`:

```sql
CREATE TRIGGER IF NOT EXISTS files_vec_delete AFTER DELETE ON files BEGIN
    DELETE FROM files_vec WHERE rowid = old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS file_chunks_vec_delete AFTER DELETE ON file_chunks BEGIN
    DELETE FROM file_chunks_vec WHERE rowid = old.id;
END;
```

These are added to the `if len(vec_tables_for_triggers) == 5:` block (changed from `== 3`).

### 2.4 Migration ordering

Within the single startup migration block in `ouvrage/db/schema.py`:

1. `tasks.merged_at` ALTER + backfill — touches existing data, must run before any new feature reads it.
2. `files.role` ALTER + index — required before `reference_doc_versions.file_id` validation can rely on role.
3. `reference_doc_configs` table.
4. `reference_doc_versions` table (FK to configs and files).
5. `projects.living_docs_config` ALTER.
6. `files_embeddings`, `file_chunks` tables.
7. `files_vec`, `file_chunks_vec` virtual tables (in try/except).
8. vec delete triggers (only when all 5 vec tables exist).

This ordering is robust against partial application — every step is idempotent (`IF NOT EXISTS`, `ADD COLUMN` guarded by `PRAGMA table_info`).

### 2.5 Indexes summary

| Index | Purpose |
|---|---|
| `idx_refdoc_configs_project` | dashboard lists docs per project |
| `idx_refdoc_versions_config(config_id, created_at)` | history view for a single doc |
| `idx_refdoc_versions_file` | reverse lookup file → version (used by file-delete cascade and search current-version filter) |
| `idx_files_role` (partial, role='reference_doc') | the `scope=docs` search path filters on this; partial keeps the regular files index lean |
| `idx_file_chunks_file` | re-chunking on regen deletes by file_id |

---

## 3. Service class shape

New file: `ouvrage/services/living_docs.py`. Pattern mirrors how dispatch logic is split today — thin DB helpers + a small service that orchestrates them and the embedding hook.

```python
# ouvrage/services/living_docs.py
import asyncio
import json
import uuid

import ouvrage.db as db
from ouvrage.db._helpers import now_iso
from ouvrage.db.search import index_doc_file


class LivingDocsService:
    """Orchestrates reference doc configs, versions, and the embedding hook.

    DB operations delegate to ouvrage.db.reference_docs.* helpers.
    File storage delegates to the existing files subsystem (db.create_file,
    UPLOADS_DIR convention from files_handler.py).
    Vector indexing delegates to ouvrage.db.search.index_doc_file (fire-and-forget).
    Task dispatch delegates to ouvrage.dispatch.engine.dispatch_task.
    """

    # --- config CRUD ---
    async def set_config(
        self, project_id: str, name: str, title: str, brief: str,
        audience: str | None = None, paused: bool = False, user_id: int | None = None,
    ) -> dict:
        """Upsert a reference_doc_configs row. Idempotent on (project_id, name)."""
        ...

    async def get_config(self, config_id: str) -> dict | None:
        ...

    async def list_configs(self, project_id: str) -> list[dict]:
        ...

    async def delete_config(self, config_id: str) -> None:
        """Cascade: delete file rows for all versions, then delete config (CASCADE removes versions)."""
        ...

    # --- versions ---
    async def add_version(
        self, config_id: str, source_path: str, task_id: str | None,
        citations: list[dict], note: str | None, unchanged: bool = False,
        worktree_root: str | None = None,
    ) -> dict:
        """Copy the .md file into UPLOADS_DIR, create a files row with role='reference_doc',
        insert a reference_doc_versions row, advance current_version_id, fire the embedding hook.

        - Validates source_path is within worktree_root when called from a worker.
        - Validates source filename ends in .md.
        - Refuses if config is paused.
        """
        ...
        asyncio.create_task(index_doc_file(file_record["id"]))
        return version_record

    async def get_current_version(self, config_id: str) -> dict | None:
        ...

    async def list_versions(self, config_id: str, limit: int = 50) -> list[dict]:
        ...

    # --- regen orchestration ---
    async def regenerate(
        self, project_id: str, config_id: str | None = None,
        force: bool = False, model: str = "opus", user_id: int | None = None,
    ) -> dict:
        """Compute the regen prompt, dispatch a regen task, return its id.

        - If config_id is None: regenerate every config in this project that is
          not paused and is stale (per project.living_docs_config thresholds).
        - Otherwise regenerate only that config (still respects paused unless force=True).
        - Builds the system prompt by calling _build_regen_prompt(config, project, since_iso).
        - Calls ouvrage.dispatch.engine.dispatch_task(...) with auto_test=False, auto_review=False
          (regen task validates itself by calling add_reference_doc_version).
        """
        ...

    async def staleness(self, config_id: str) -> dict:
        """Return {merged_since_count, days_since_regen, is_stale, reason}."""
        ...

    # --- search support ---
    async def current_version_file_ids(self, project_id: str | None) -> list[str]:
        """Returns file_ids whitelist for scope=docs filter (current versions only)."""
        ...
```

### What this service owns vs delegates

| Concern | Owner | Notes |
|---|---|---|
| `reference_doc_configs` / `reference_doc_versions` rows | `ouvrage/db/reference_docs.py` (new) | thin async functions; no business logic |
| File storage on disk | existing `ouvrage/server/handlers/files_handler.py` UPLOADS_DIR convention | reuse `db.create_file` + path-validation logic; a small private helper `_copy_into_uploads(file_path, role)` lives in the service |
| Vector indexing | `ouvrage/db/search.py:index_doc_file` (new) | fire-and-forget via `asyncio.create_task`; same pattern as message chunking |
| Search query | `ouvrage/db/search.py:search_files_semantic` + `search_file_chunks_semantic` (new) | called by `_handle_search` (handlers/search.py) |
| Task dispatch | `ouvrage/dispatch/engine.dispatch_task` (existing) | regen task is a normal CC task with a special system-prompt prefix |
| Cron loop | `ouvrage/dispatch/living_docs_sweep.py` (new) | pattern from `pr_sweep.py`; calls `LivingDocsService.regenerate` |
| Completion hook | `ouvrage/dispatch/lifecycle.py` post-gate-pass branch | already fires; we add a small handler that updates `last_regen_at`/`last_regen_task_id` and verifies the regen task called `add_reference_doc_version` at least once |

The service is stateless — every method opens its own DB connection. No singletons.

---

## 4. MCP tool specs

All three tools route through `ouvrage/server/dispatch.py:TOOL_HANDLERS`. Schemas live in `ouvrage/server/tools.py` (added to the FILES_TOOLS group, since they extend the file/doc story; could equally live in a new `LIVING_DOCS_TOOLS` group). Handlers live in a new file `ouvrage/server/handlers/living_docs_handler.py`.

### 4.1 `set_reference_doc_config`

User-callable. Worker-callable too (workers may want to refine briefs from inside a discovery task).

```python
Tool(
    name="set_reference_doc_config",
    description=(
        "Create or update a Living Docs reference-doc config for a project. "
        "Briefs are durable curated input that drive regeneration. "
        "Idempotent on (project_id, name)."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "name":       {"type": "string", "description": "short slug, e.g. 'auth-overview'", "pattern": "^[a-z0-9][a-z0-9-]{0,63}$"},
            "title":      {"type": "string"},
            "brief":      {"type": "string", "description": "Curated description of what this doc covers and at what level."},
            "audience":   {"type": "string", "description": "Optional voice hint, e.g. 'new contributors'"},
            "paused":     {"type": "boolean", "default": False},
        },
        "required": ["project_id", "name", "title", "brief"],
    },
),
```

**Handler `_handle_set_reference_doc_config`:**
- Validates `project_id` exists.
- Validates `name` matches `^[a-z0-9][a-z0-9-]{0,63}$` (already in schema regex but enforce explicitly to give a better error message).
- Validates `brief` is non-empty and ≤ 8000 chars.
- Calls `LivingDocsService.set_config(...)` (upsert).
- Returns the resulting config row plus `staleness` info.

**Worker-only flag:** none — both endpoints can call.

### 4.2 `regenerate_reference_docs`

User-callable. Optionally worker-callable for chained regen tasks.

```python
Tool(
    name="regenerate_reference_docs",
    description=(
        "Trigger reference-doc regeneration for a project. Creates a regen task "
        "(model=opus by default) for each stale or explicitly named config. "
        "Returns the dispatched task ids."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string"},
            "config_name": {"type": "string", "description": "Optional — restrict to one config by name."},
            "force": {"type": "boolean", "default": False, "description": "Regenerate even if not stale."},
            "model": {"type": "string", "enum": ["opus", "sonnet"], "default": "opus"},
        },
        "required": ["project_id"],
    },
),
```

**Handler `_handle_regenerate_reference_docs`:**
- Resolve `config_id` from `config_name` if provided.
- Computes staleness via `LivingDocsService.staleness(...)`. If `force=False` and not stale, returns `{dispatched: [], skipped: [...], reason: "not stale"}` per config.
- For each stale config: calls `LivingDocsService.regenerate(...)` which dispatches a regen task via `engine.dispatch_task`.
- Returns `{dispatched: [task_id, ...], skipped: [{config_id, reason}, ...]}`.
- Validation: refuses if `project.paused` or `living_docs_config.paused` is set.

**Worker-only flag:** none.

### 4.3 `add_reference_doc_version`

**Worker-only.** This is the tool the regen task calls when it has produced an updated `.md`.

```python
Tool(
    name="add_reference_doc_version",
    description=(
        "Worker-only. Persist a new reference-doc version produced by a regen task. "
        "Copies the .md file into Ouvrage storage, links it to the config, advances "
        "current_version_id, and triggers vector indexing. "
        "Pass unchanged=true with no source_path when nothing material changed."
    ),
    inputSchema={
        "type": "object",
        "properties": {
            "task_id":     {"type": "string", "description": "the regen task's id (worktree validation)"},
            "config_id":   {"type": "string"},
            "source_path": {"type": "string", "description": "absolute path within worktree to the .md file"},
            "citations":   {
                "type": "array",
                "description": "Sources cited in this version. Each item: {kind, ref, sha?, message_id?, task_id?}",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind":       {"type": "string", "enum": ["code", "conversation", "task"]},
                        "ref":        {"type": "string"},
                        "sha":        {"type": "string"},
                        "message_id": {"type": "integer"},
                        "task_id":    {"type": "string"},
                    },
                    "required": ["kind", "ref"],
                },
            },
            "note":      {"type": "string", "description": "1-3 sentences summarising what changed."},
            "unchanged": {"type": "boolean", "default": False},
        },
        "required": ["task_id", "config_id"],
    },
),
```

**Handler `_handle_add_reference_doc_version`:**
- Worker check: `if not get_request_is_worker(): raise ValueError("add_reference_doc_version is only available on the worker endpoint")` (mirror `_handle_add_task_file:90`).
- Validates `task_id` exists and the config belongs to the same project as the task.
- If `unchanged=True`: insert a row with `unchanged=True`, no file. Do **not** advance `current_version_id`. This is how the agent records "I checked and nothing changed."
- Else: validates `source_path` is within `task.worktree_path` (mirror the resolve+`relative_to` check in `files_handler.py:117`). Validates extension is `.md`. Calls `LivingDocsService.add_version(...)`.
- Returns `{version_id, file_id, current_version_id, citations: [...], unchanged}`.

**Worker-only flag:** yes. Add `"add_reference_doc_version"` to `WORKER_TOOL_ALLOWLIST` in `ouvrage/server/tools.py:1054`.

### 4.4 Tool registration summary

| Tool | Schema location | Handler file | Routing entry | Worker-only? |
|---|---|---|---|---|
| `set_reference_doc_config` | `ouvrage/server/tools.py` (new `LIVING_DOCS_TOOLS` group) | `ouvrage/server/handlers/living_docs_handler.py` | `dispatch.py: TOOL_HANDLERS["set_reference_doc_config"]` | No |
| `regenerate_reference_docs` | same | same | same | No |
| `add_reference_doc_version` | same | same | same | **Yes** — added to `WORKER_TOOL_ALLOWLIST` |

Both `LIVING_DOCS_TOOLS` and the imports in `dispatch.py` follow the existing `from ... import _handle_xxx` pattern (compare `dispatch.py:59–65` for files).

---

## 5. System prompt for regen

This prompt is **hardcoded** in `ouvrage/services/living_docs.py:_REGEN_SYSTEM_PROMPT_TEMPLATE`. The template has three placeholders filled at dispatch time (§6):

- `{config_block}` — config name, title, brief, audience, current version excerpt
- `{change_summary}` — list of merged tasks (id, goal, branch, merged_at) + recently-modified files since last regen
- `{citation_examples}` — concrete citation strings drawn from the project (a few real `code:path@sha` pointers harvested at dispatch)

```
You are the Living Docs regenerator for the Ouvrage project. Your job is
to produce or refresh ONE reference document for a project. Reference docs
are the curated, agent-maintained "what you'd want a new contributor to
read first" view of a slice of the codebase.

Your output is a single Markdown file written via the
`add_reference_doc_version` MCP tool. You DO NOT commit, push, or modify
any other files. The only side effects you produce are tool calls.

────────────────────────────────────────────────────────────────────────
DOC YOU ARE REGENERATING
────────────────────────────────────────────────────────────────────────
{config_block}

────────────────────────────────────────────────────────────────────────
WHAT HAPPENED SINCE THE LAST REGEN
────────────────────────────────────────────────────────────────────────
{change_summary}

────────────────────────────────────────────────────────────────────────
HOW TO WORK
────────────────────────────────────────────────────────────────────────

1. Read the current version (provided above) and the brief.
2. Read the merged-PR list above and inspect the actual diffs in the
   worktree using `git log`, `git diff`, and direct file reads.
3. Decide whether the doc needs material change. Apply the UNCHANGED
   criteria below — do not rewrite for the sake of it.
4. If material change is needed: produce a new Markdown file at
   `/work/<task-worktree>/<config-name>.md`, then call
   `add_reference_doc_version` with that path.
5. If no material change is needed: call `add_reference_doc_version` with
   `unchanged=true` and a one-sentence note explaining why. Do not
   produce a file.

You are NOT writing release notes. You are NOT writing a changelog. You
are refreshing a stable reference document that someone reading it cold
should be able to use to understand the slice. The "what changed" log
above is your input, not your output.

────────────────────────────────────────────────────────────────────────
VOICE
────────────────────────────────────────────────────────────────────────

Voice is terse, present-tense, technical. Audience is a future
contributor who has read the rest of the project's CLAUDE.md but not
this slice. Default to ~600–1500 words; longer is fine if the slice
warrants it. Do not pad. Do not hedge. Do not include phrases like
"In this document, we will explore…" — get to the point.

A good reference doc for a slice has, at minimum, these top-level
sections (use `## ` headers — the chunker splits on these):

  ## Overview
  ## Architecture
  ## Data flow
  ## Interfaces
  ## Risks & gotchas
  ## Recent changes
  ## Open questions

You may add more `## ` sections if the slice needs them. You may use
`### ` subsections freely. Do NOT use `# ` (the title is the file
identity, not its first heading) and do NOT use `#### ` or deeper —
they don't get chunked.

────────────────────────────────────────────────────────────────────────
CITATIONS
────────────────────────────────────────────────────────────────────────

EVERY non-trivial claim cites a source. Citations are inline, not at
the end. Use one of three citation forms:

  - `code:<path>@<sha>` — points to a file at a commit. Use the SHA
    of the most recent main-branch commit that touches the file.
    Example: `code:ouvrage/dispatch/lifecycle.py@7b24053`
  - `conversation:<id>#<message_id>` — points to a specific message
    in a project conversation.
    Example: `conversation:living-docs#7855`
  - `task:<id>` — points to a completed task (its messages and diff).
    Example: `task:mcp-switchboard/search-decay-rework`

Formatting: cite as plain inline backtick-text, after the claim. If
multiple sources back the same claim, list them comma-separated:
`code:foo.py@abc1234, task:mcp-switchboard/foo`.

A few real citation seeds for this project:
{citation_examples}

If you cannot ground a claim, OMIT THE CLAIM. A doc without a
speculation is better than a doc with one.

────────────────────────────────────────────────────────────────────────
DIAGRAMS
────────────────────────────────────────────────────────────────────────

Prefer diagrams over prose for: state machines, request flow, table
relationships, dependency graphs. Use Mermaid when the diagram is
graph-like; use ASCII tables/boxes for small structures and column
mappings.

Mermaid example:

  ```mermaid
  stateDiagram-v2
      [*] --> ready
      ready --> working: dispatch
      working --> validating: complete
      validating --> completed: gate_pass
      validating --> stopped: gate_fail
      stopped --> working: resume
      completed --> stopped: reopen
  ```

ASCII example for a small data-flow:

  ```
  user → /mcp endpoint → dispatch.py → handler.py → db.py
                                  ↑
                          tools.py (schemas)
  ```

Keep diagrams accurate to the current code, not aspirational. If the
shape is uncertain, write prose.

────────────────────────────────────────────────────────────────────────
UNCHANGED CRITERIA
────────────────────────────────────────────────────────────────────────

Call `add_reference_doc_version(unchanged=true)` if and only if ALL of
these hold:

  1. The diff of merged work since the last regen does not touch any
     file or behavior the doc describes by name.
  2. No new public surface (function, MCP tool, schema column, route,
     CLI flag, env var) was introduced in the slice.
  3. No invariant or constraint stated in the doc has been violated
     or strengthened.
  4. The Recent changes section would not need a new bullet point.

Otherwise produce a new version. "Cosmetic" rewrites (rephrasing
without new information) count as material only if the existing
phrasing is wrong or ambiguous.

────────────────────────────────────────────────────────────────────────
THE add_reference_doc_version TOOL CALL
────────────────────────────────────────────────────────────────────────

When you finish, call exactly ONE of:

  add_reference_doc_version(
      task_id="<this-task-id>",
      config_id="<config-id-from-prompt>",
      source_path="/work/<worktree>/<config-name>.md",
      citations=[
          {"kind": "code",         "ref": "ouvrage/dispatch/lifecycle.py", "sha": "7b24053"},
          {"kind": "conversation", "ref": "living-docs",                   "message_id": 7855},
          {"kind": "task",         "ref": "mcp-switchboard/search-decay-rework"}
      ],
      note="Refreshed Architecture section to reflect new lifecycle states added in search-decay-rework."
  )

OR (when nothing material changed):

  add_reference_doc_version(
      task_id="<this-task-id>",
      config_id="<config-id-from-prompt>",
      unchanged=true,
      note="No public surface or doc-described behavior changed in this window."
  )

After this tool call returns, your work is done. Do NOT make further
edits. Do NOT push. Do NOT post a result message — Ouvrage will record
the version and post a status message automatically. Exit cleanly.
```

---

## 6. Regen task wiring

### 6.1 Cron loop

New file `ouvrage/dispatch/living_docs_sweep.py`, modelled on `ouvrage/dispatch/pr_sweep.py:90`:

```python
import asyncio, logging
import ouvrage.db as db
from ouvrage.services.living_docs import LivingDocsService

log = logging.getLogger(__name__)
SWEEP_INTERVAL = 60 * 30  # 30 minutes

async def _living_docs_sweep() -> None:
    service = LivingDocsService()
    while True:
        await asyncio.sleep(SWEEP_INTERVAL)
        try:
            projects = await db.list_projects()
        except Exception as e:
            log.warning("living-docs sweep: list_projects failed: %s", e)
            continue

        for project in projects:
            if project.get("paused"):
                continue
            cfg = (project.get("living_docs_config") or {})
            if cfg.get("paused"):
                continue
            try:
                # service.regenerate is a no-op for non-stale configs unless force=True
                await service.regenerate(project_id=project["id"], force=False)
            except Exception as e:
                log.warning("living-docs sweep: regenerate %s failed: %s", project["id"], e)
```

Started in the lifespan handler `ouvrage/server/app.py:485–499` alongside the existing background tasks:

```python
asyncio.create_task(_living_docs_sweep())
```

### 6.2 Staleness rules

`LivingDocsService.staleness(config_id)` returns `{merged_since_count, days_since_regen, is_stale, reason}`. A config is stale iff:

- `merged_since_count >= project.living_docs_config.min_merges_to_regen` (default **5**), OR
- `days_since_regen >= project.living_docs_config.max_age_days` (default **14**)

These thresholds are defaults; per-project config in `projects.living_docs_config` overrides. `force=True` short-circuits both.

### 6.3 Prompt assembly at dispatch time

`LivingDocsService.regenerate(...)`:

1. Resolves the configs to regenerate (one or many).
2. For each config:
   - Load `current_version` (file content if it exists, else "(no current version — bootstrap regen)").
   - Load `change_summary`: results of `db.list_merged_tasks_since(project_id, config.last_regen_at)` → format as a short bulleted list of `task_id, goal, branch, merged_at, pr_url`.
   - Load `citation_examples`: 3 real entries derived from the project's recent code (e.g., from `git log --pretty=format:'%H %s' -10 origin/main` extracted at dispatch time) plus pinned messages from any related conversation if `conversation_id` is set on a recent task.
   - Format the system prompt by `format_map`-ing into `_REGEN_SYSTEM_PROMPT_TEMPLATE`.
3. Calls `engine.dispatch_task(...)`:

   ```python
   await dispatch_task(
       id=f"{project_id}/refdocs/{config['name']}/{short_iso_now}",
       project_id=project_id,
       goal=f"Regenerate Living Doc: {config['title']}",
       model=cfg.get("regen_model", "opus"),
       auto_test=False,           # no test suite for a doc-only task
       auto_review=False,         # review gate would inject the wrong feedback loop
       auto_pr=False,             # nothing to PR — output goes through add_reference_doc_version
       conversation_id=None,
       parent_task_id=None,
       max_turns=120,
       max_wall_clock=30,         # minutes
       system_prompt_prepend=rendered_prompt,  # see note below
   )
   ```

   `system_prompt_prepend` is a new dispatch parameter (small addition in `engine.dispatch_task` and `dispatch/sdk_session.py` — already structured to accept extra prompt context per the existing CC SDK harness). If reviewers prefer not to add a parameter, the prompt can be wrapped into the `goal` field's task spec instead — the regen task's `goal` line becomes a marker and the full prompt sits in the spec body.

4. Records `last_regen_task_id` on the config (for traceability), but **does not** advance `current_version_id` until the task completes.

### 6.4 Completion hook

The lifecycle gate-pass code (or, if `auto_test=auto_review=False`, the equivalent task-completion path in `ouvrage/dispatch/lifecycle.py`) needs a small post-completion handler for regen tasks:

```python
# in ouvrage/dispatch/lifecycle.py, after the existing on-complete branch
if task.get("goal", "").startswith("Regenerate Living Doc:"):
    from ouvrage.services.living_docs import LivingDocsService
    await LivingDocsService().on_regen_complete(task_id=task["id"])
```

`on_regen_complete` does:

1. Looks up the config from `last_regen_task_id`.
2. Verifies that the regen task called `add_reference_doc_version` at least once during its run (i.e., `reference_doc_versions WHERE task_id = ? LIMIT 1` returns a row). If not, posts a status message and leaves `last_regen_at` unchanged so the next sweep retries.
3. If the most recent version inserted has `unchanged=True`, updates only `last_regen_at` (timestamp) — `current_version_id` stays put.
4. Otherwise advances `config.current_version_id = newest_version_id` and updates `last_regen_at`.
5. Posts a `status` message to the task thread describing the outcome.

`add_reference_doc_version`'s handler (§4.3) is what actually inserts the version row and the file — the completion hook only finalizes the config pointer. This split keeps the worker tool idempotent and the lifecycle hook simple.

---

## 7. Search integration

### 7.1 Embedding hook placement

Single point of entry: `LivingDocsService.add_version` calls `asyncio.create_task(index_doc_file(file_id))` (§3). `index_doc_file` lives in `ouvrage/db/search.py` and mirrors `index_message_chunks`. Re-runs are idempotent (delete-by-`file_id` first).

A startup backfill loop covers files added when the embedding service was unavailable:

```python
# ouvrage/server/app.py — alongside _backfill_message_chunks
asyncio.create_task(_backfill_file_chunks())

async def _backfill_file_chunks():
    while True:
        await asyncio.sleep(60)
        try:
            file_ids = await db.get_doc_files_needing_chunking(batch_size=20)
            for fid in file_ids:
                try:
                    await index_doc_file(fid)
                except Exception as e:
                    log.warning("doc backfill failed for %s: %s", fid, e)
        except Exception as e:
            log.warning("doc backfill loop error: %s", e)
```

`get_doc_files_needing_chunking` mirrors `get_messages_needing_chunking` (`ouvrage/db/search.py:938`).

### 7.2 vec0 tables for files

Already covered in §1.3 / §2.1. Two virtual tables: `files_vec` (whole-file embeddings, rowid = `files.rowid`) and `file_chunks_vec` (chunk embeddings, rowid = `file_chunks.id`).

### 7.3 `scope=docs` filter

Add a `scope` argument to `_handle_search` (`ouvrage/server/handlers/search.py:96`):

```python
scope = arguments.get("scope")  # "docs" | "messages" | None (= all)
project_id = arguments.get("project_id")
```

In the parallel `asyncio.gather` block:

```python
if scope is None or scope == "docs":
    file_chunk_hits, file_whole_hits = await asyncio.gather(
        db.search_file_chunks_semantic(query_vector, project_id=project_id, current_only=True, limit=limit),
        db.search_files_semantic(query_vector, project_id=project_id, current_only=True, limit=limit),
    )
else:
    file_chunk_hits, file_whole_hits = [], []

if scope == "docs":
    fts_msg_hits = vec_msg_hits = fts_task_hits = vec_task_hits = chunk_hits = []
```

Doc candidates are then folded into the unified candidate list with their default weights:

```python
DOC_CURRENT_WEIGHT = 1.6
DOC_PRIOR_WEIGHT   = 0.5
TASK_ARTIFACT_WEIGHT = 1.0  # applies to non-doc files (uploads attached to tasks)
```

For each doc hit, the entity_id is the `files.id`; the manual-override key is `("doc_version", file_id)` (whole file) or `("doc_chunk", chunk_id)` (chunk). The recency multiplier is computed off `files.created_at`, the same way it is for messages.

`current_only=True` translates to the JOIN filter shown in §1.3. When `current_only=False`, a hit on a prior version is multiplied by `DOC_PRIOR_WEIGHT` rather than `DOC_CURRENT_WEIGHT`.

### 7.4 Search weights expansion

Update `ouvrage/db/search_weights.py:6`:

```python
_VALID_ENTITY_TYPES = {"task", "message", "chunk", "doc_version", "doc_chunk"}
```

Update `_handle_set_weight` schema (`ouvrage/server/handlers/search.py:335`) to allow the new types in its description, but no schema-shape change required.

The default-weight constants live in `ouvrage/server/handlers/search.py` next to `_TYPE_BOOST`:

```python
_DOC_CURRENT_BOOST = 1.6
_DOC_PRIOR_BOOST   = 0.5
_TASK_ARTIFACT_BOOST = 1.0  # plain task .md files (role='upload', task-scoped)
```

The unified scorer applies them where it currently applies `_TYPE_BOOST` for messages.

---

## 8. Implementation chain proposal

Each row below is a proposed task. IDs are concrete `mcp-switchboard/<slug>` strings that downstream automation can use directly. `depends_on` is the parent task that must reach gate-pass before the next can dispatch (per Ouvrage chain semantics).

| # | Task ID | Model | Depends on | Scope (one line) |
|---|---|---|---|---|
| 1 | `mcp-switchboard/living-docs-merged-at` | sonnet | — | Add `tasks.merged_at` column, backfill from `pushed_at`, set in auto-merge and pr-sweep, helper `list_merged_tasks_since`. |
| 2 | `mcp-switchboard/living-docs-schema` | sonnet | (1) | Add `reference_doc_configs`, `reference_doc_versions`, `files.role`, `projects.living_docs_config`; indexes; vec0 file tables and triggers. |
| 3 | `mcp-switchboard/living-docs-files-role` | sonnet | (2) | Wire delete-protection in `db/files.py:delete_file` for `role='reference_doc'`; add `delete_reference_doc_files()` bypass for cascade. Update tool handlers to surface role in `list_files`/`get_file` responses. |
| 4 | `mcp-switchboard/living-docs-db-helpers` | sonnet | (2) | Implement `ouvrage/db/reference_docs.py` with config and version CRUD. No service or MCP layer yet. |
| 5 | `mcp-switchboard/living-docs-embeddings` | sonnet | (2) | Implement `index_doc_file`, `set_file_embedding`, `search_files_semantic`, `search_file_chunks_semantic`, `get_doc_files_needing_chunking` in `ouvrage/db/search.py`. Add `_backfill_file_chunks` loop in `app.py`. |
| 6 | `mcp-switchboard/living-docs-service` | sonnet | (3, 4, 5) | Implement `LivingDocsService` in `ouvrage/services/living_docs.py` (config CRUD, add_version, staleness, regenerate orchestration scaffolding — not the cron loop yet). |
| 7 | `mcp-switchboard/living-docs-mcp-tools` | sonnet | (6) | Implement `set_reference_doc_config`, `add_reference_doc_version` MCP tools in `ouvrage/server/handlers/living_docs_handler.py`; add to `tools.py`, `dispatch.py`, `WORKER_TOOL_ALLOWLIST`. (Defer `regenerate_reference_docs` to the next task because it depends on dispatch wiring.) |
| 8 | `mcp-switchboard/living-docs-search-integration` | sonnet | (5, 7) | Extend `_handle_search` with `scope=docs`, current_only filter, doc weights. Update `search_weights._VALID_ENTITY_TYPES` and weight tool description. |
| 9 | `mcp-switchboard/living-docs-regen-prompt` | opus | (6) | Land the hardcoded system prompt template in `ouvrage/services/living_docs.py:_REGEN_SYSTEM_PROMPT_TEMPLATE` plus the prompt-assembly helper `_build_regen_prompt(config, project, since_iso)`. |
| 10 | `mcp-switchboard/living-docs-dispatch-wiring` | sonnet | (7, 9) | Implement `LivingDocsService.regenerate` (programmatic dispatch via `engine.dispatch_task`), the completion hook in `lifecycle.py`, and the `regenerate_reference_docs` MCP tool. Add `system_prompt_prepend` parameter to `dispatch_task` (or equivalent injection mechanism — see §6.3 note). |
| 11 | `mcp-switchboard/living-docs-cron` | sonnet | (10) | Implement `_living_docs_sweep` in `ouvrage/dispatch/living_docs_sweep.py`; register in `app.py` lifespan. |
| 12 | `mcp-switchboard/living-docs-bootstrap-test` | opus | (11) | End-to-end smoke: register one config for this project, force-regenerate, verify version row + file + embeddings + search hit. Documented as a one-off task; not added as automated test. |

### Sequential vs parallel

```
1 ──► 2 ──┬──► 3 ──┐
          ├──► 4 ──┼──► 6 ──┬──► 7 ──┬──► 8
          └──► 5 ──┘        ├──► 9   └──► 10 ──► 11 ──► 12
                                       (10 also depends on 7)
```

- **Sequential (file conflict)**: 1 → 2 (both touch `db/schema.py`); 2 → 3 (both touch `db/files.py` migration + delete path); 6 → 9 (both touch `services/living_docs.py`); 7 → 8 (both touch `server/handlers/search.py`); 10 → 11 (both touch lifespan/dispatch glue).
- **Parallelizable**: 3, 4, 5 all only depend on 2 and touch separate files (`files.py`/handler, `reference_docs.py`, `search.py`). They can run concurrently.
- **Parallelizable**: 9 depends on 6 only and touches a different file from 7/8; can run beside the MCP-tools branch.

### Acceptance criteria (per task)

| # | Acceptance |
|---|---|
| 1 | `merged_at` set in both merge paths; query helper returns expected rows; existing test suite still passes. |
| 2 | New tables exist after migration; `PRAGMA table_info` confirms `files.role` and `projects.living_docs_config`; vec0 virtual tables created when sqlite-vec is loaded; idempotent on rerun. |
| 3 | `delete_file` raises `ValueError` when `role='reference_doc'`; cascade path (`delete_reference_doc_files`) deletes file rows and disk files. Unit tests in `tests/test_files_role.py`. |
| 4 | All 6 service-shape methods reachable via the new `db/reference_docs.py` module. Tests cover create/list/get/delete and the unique constraint on `(project_id, name)`. |
| 5 | A `.md` file inserted with `role='reference_doc'` lands in `files_embeddings`, `file_chunks`, `files_vec`, `file_chunks_vec` end-to-end. `search_files_semantic` returns it for a relevant query. |
| 6 | Service can `set_config`, `add_version` (with embedding hook firing), `staleness` returns expected booleans against synthetic merged-task data. |
| 7 | `set_reference_doc_config` tool roundtrips a config from the user endpoint; `add_reference_doc_version` works only on the worker endpoint and rejects out-of-worktree paths. |
| 8 | `_handle_search(scope='docs', project_id=...)` returns only doc hits, current versions only by default, with the right weight ratios. |
| 9 | Prompt template renders with realistic `{config_block}`, `{change_summary}`, `{citation_examples}`. Snapshot test in `tests/test_living_docs_prompt.py`. |
| 10 | `regenerate_reference_docs` dispatches a real task; completion hook updates `current_version_id` only when a non-`unchanged` version is inserted. |
| 11 | Cron loop runs once per `SWEEP_INTERVAL` per project, respects `paused`, dispatches only stale configs. |
| 12 | Manual run produces a real refreshed reference doc; UI/API can fetch it back; search returns it for a topical query. |

### Models

`opus` for the prompt-template task (9) and the bootstrap-test task (12); `sonnet` is enough for everything else (mostly schema and wiring). The regen tasks themselves run `opus` by default (set in `LivingDocsService.regenerate`).

---

## 9. Bootstrap tooling

For the human + LLM to author the first project's brief list productively, the **only required tool is `set_reference_doc_config`**. The workflow is:

1. Human pairs with an LLM on the `living-docs` (or per-project equivalent) conversation, drafting a list of brief slugs and titles.
2. LLM calls `set_reference_doc_config` once per slug, dropping in a curated brief.
3. Human triggers the first wave with `regenerate_reference_docs(project_id=..., force=true)`.
4. Cron takes over after that.

**Things that would help but are not blockers:**

- A small dashboard view to list configs and their staleness (`/dashboard/projects/<id>/docs`). This is explicitly out of scope per the spec but would be the first follow-on after launch.
- A `list_reference_doc_configs` MCP tool. Strictly speaking the dashboard already gets this via the unified API; LLM access is the ask. Easy to add as a nicety in the MCP-tools task (#7) — recommend folding it in.

**Recommend folding into task #7:** `list_reference_doc_configs(project_id)` and `get_reference_doc_config(config_id)` — both user-callable, both thin reads, no worker-only flag. This makes the LLM-drafting loop materially easier without expanding scope.

---

## 10. Risks and unknowns

### Things to verify after the first regen wave

1. **Prompt drift.** Models may invent citation SHAs that don't exist. After the first wave, audit each version's `citations_json` against `git log` and the conversations referenced. If false citations are common, tighten the prompt to require `citations` to be ground-truthed against tool calls (`git log`, `read_task_messages`) made earlier in the same session.
2. **Search weight calibration.** The 1.6 / 1.0 / 0.5 / 1.0 split is a guess. After ~50 real searches, look at the result mix in the dashboard: are doc hits showing up at the right rate? If docs dominate trivially or never appear, retune in 0.2 increments.
3. **File-vs-message dedup.** Some docs will repeat content from pinned spec messages. The unified scorer dedupes by `entity_id` (`ouvrage/server/handlers/search.py:323`), but doc and message ids never collide, so a query may surface both. Decide whether to add a near-duplicate suppressor (cosine > 0.9 → drop the lower-weighted) only after we observe it being a real problem.
4. **`unchanged=True` calibration.** If models call `unchanged` too often, the docs ossify. If they call it too rarely, churn drowns out signal. Track ratio per project; if `unchanged` ratio drops below 20% we're regenerating too aggressively.
5. **Regen cost.** Opus regen tasks are expensive. The 30-minute cron + 5-merges/14-days threshold is conservative. Monitor `tasks.total_cost_usd` for tasks whose goal starts with `Regenerate Living Doc:` over the first month and tune thresholds in `living_docs_config`.
6. **Worktree isolation for regen.** Regen tasks need to read the **main** branch of the repo, not their own branch. Two options: (a) allow `auto_release_worktree=False` and have the regen task `git fetch origin && git checkout origin/main` read-only at the start, or (b) skip the worktree entirely and have the system prompt provide a pre-collected diff bundle. Option (a) integrates better with existing dispatch; flag for the dispatch-wiring task to confirm.

### Things that might need to be revised

- **`merged_at` backfill from `pushed_at` is approximate.** Tasks that auto-merged via PR sweep have `pushed_at` ≈ original push time, not merge time. The first regen for a long-lived project will see merges "since the dawn of time" because all `merged_at` will be near the migration timestamp. If this matters, add a one-off sync that pulls `merged_at` from the GitHub API for closed PRs at migration time. Defaulted out — the next merge populates it correctly.
- **`add_reference_doc_version(unchanged=True)` semantics.** The current proposal records an unchanged version as a row with no file. An alternative is to *not* record anything and just bump `last_regen_at`. The row-with-no-file design preserves a trail ("we checked at 2026-04-28 and nothing changed"); the alternative is leaner but loses that signal. Stick with row-with-no-file unless reviewers prefer otherwise.
- **`system_prompt_prepend` parameter.** Adding a parameter to `engine.dispatch_task` is simple but expands the dispatch surface. The alternative is to put the prompt in the task `goal` / spec body. Either works; the parameter is cleaner for future regen-style tasks (e.g., a "release notes regen" task could reuse the same plumbing).
- **Doc encoding for the chunker.** The chunker (`ouvrage/embeddings/chunks.py`) splits only on `## ` and `### `. The system prompt enforces that doc structure — but if a model generates a `# Title` line at the top followed only by `### ` subheadings, the chunker may produce only one section and refuse to chunk. The `_handle_add_reference_doc_version` validator should warn (not reject) when the produced file would not chunk, and the `index_doc_file` sentinel keeps the file searchable via the whole-file embedding regardless.

---

## Appendix A — Files touched by this plan (when implemented)

| File | Reason |
|---|---|
| `ouvrage/db/schema.py` | Migrations: `files.role`, `tasks.merged_at`, `projects.living_docs_config`, two new tables, four new vec/embedding tables, triggers |
| `ouvrage/db/files.py` | Delete-protection guard for `role='reference_doc'` |
| `ouvrage/db/reference_docs.py` (new) | Config + version CRUD |
| `ouvrage/db/search.py` | `index_doc_file`, `search_files_semantic`, `search_file_chunks_semantic`, `set_file_embedding`, `get_doc_files_needing_chunking` |
| `ouvrage/db/search_weights.py` | `_VALID_ENTITY_TYPES` += `doc_version`, `doc_chunk` |
| `ouvrage/db/tasks.py` | `list_merged_tasks_since`, `merged_at` in update path |
| `ouvrage/services/living_docs.py` (new) | `LivingDocsService` + `_REGEN_SYSTEM_PROMPT_TEMPLATE` + `_build_regen_prompt` |
| `ouvrage/server/handlers/living_docs_handler.py` (new) | `_handle_set_reference_doc_config`, `_handle_regenerate_reference_docs`, `_handle_add_reference_doc_version`, `_handle_list_reference_doc_configs`, `_handle_get_reference_doc_config` |
| `ouvrage/server/handlers/search.py` | `scope=docs` branch, doc weight constants, doc candidate construction |
| `ouvrage/server/tools.py` | New `LIVING_DOCS_TOOLS` group; `add_reference_doc_version` added to `WORKER_TOOL_ALLOWLIST` |
| `ouvrage/server/dispatch.py` | New TOOL_HANDLERS entries |
| `ouvrage/server/app.py` | Start `_living_docs_sweep` and `_backfill_file_chunks` in lifespan |
| `ouvrage/dispatch/living_docs_sweep.py` (new) | Cron loop |
| `ouvrage/dispatch/lifecycle.py` | Completion hook for regen tasks |
| `ouvrage/dispatch/engine.py` | Optional `system_prompt_prepend` parameter |
| `ouvrage/git/operations.py` | Set `merged_at` in auto-merge update_task call |
| `ouvrage/dispatch/pr_sweep.py` | Set `merged_at` when transitioning to `pr_status='merged'` |
| `ouvrage/config/constants.py` | `merged_at` added to `TASK_MUTABLE_FIELDS` |

## Appendix B — File-path glossary

The task spec uses `switchboard/...` paths in a few places; this repo's package is `ouvrage/...`. Quick translation:

| Spec | Real |
|---|---|
| `switchboard/db/schema.py` | `ouvrage/db/schema.py` |
| `switchboard/server/handlers/files_handler.py` | `ouvrage/server/handlers/files_handler.py` |
| `switchboard/git/files.py` | `ouvrage/git/files.py` (lightweight git file ops; not central to this feature) |
| `switchboard/embeddings/chunker.py` | `ouvrage/embeddings/chunks.py` (note: file is `chunks.py`, not `chunker.py`) |
