# Search Chain Review: FTS5 + sqlite-vec + Hybrid Ranking

**Reviewer:** Opus  
**Date:** 2026-04-04  
**Branches reviewed:** fts5-indexes, sqlite-vec-migration, hybrid-search-ranking (all merged into search-chain-opus-review)

---

## Critical

### 1. Search crashes when sqlite-vec is unavailable but OPENAI_API_KEY is set

**Where:** `switchboard/db/search.py:37-39` (search_messages_semantic), also lines 496-499 (search_message_chunks), 700-703 (search_tasks_semantic)

**Problem:** The fallback logic in `_handle_search` only falls back to FTS-only when `embed_safe()` returns `None` (no API key). But if sqlite-vec fails to load (extension not installed), vec0 tables don't get created (`schema.py:572-577` catches the error silently). When a user searches with a valid API key, `embed_safe()` succeeds, `has_embeddings = True`, and the handler calls `search_messages_semantic()` which runs `SELECT rowid, distance FROM messages_vec WHERE embedding MATCH ?` against a non-existent table. This throws `sqlite3.OperationalError: no such table: messages_vec` and crashes the search handler.

**Fix:** Either:
- (a) Add `try/except` around vec0 queries in `search_messages_semantic`, `search_tasks_semantic`, and `search_message_chunks`, returning `[]` on failure, OR
- (b) Check at startup whether vec0 tables actually exist and set a module-level flag, then skip vec queries if the flag is false, OR
- (c) Wrap the `asyncio.gather` call in `_handle_search` with error handling that degrades individual search streams gracefully.

Option (a) is simplest and most resilient.

### 2. No vec0 cleanup on message or task deletion

**Where:** `switchboard/db/schema.py:614-645` (FTS5 triggers), `switchboard/db/projects.py:143` (delete path)

**Problem:** FTS5 triggers handle cleanup correctly: `messages_fts_delete` fires on `DELETE FROM messages`, and `tasks_fts_delete` fires on `DELETE FROM tasks`. However, there are NO corresponding triggers or manual cleanup for `messages_vec`, `tasks_vec`, or `chunks_vec`. When a project is deleted (`projects.py:143` does `DELETE FROM messages WHERE task_id IN (...)`), the FTS entries are cleaned up but the vec0 entries remain as orphans.

**Impact:** Orphaned vec0 rows return stale/phantom results from deleted messages. The `search_messages_semantic` function does a secondary lookup (`SELECT ... FROM messages m WHERE m.id IN (...)`) which would filter out non-existent message IDs, so the phantom results are silently dropped. This is not a crash, but:
- vec0 tables grow unboundedly with deleted data
- The oversample query wastes slots on phantom results, reducing effective result count

**Fix:** Add delete triggers for vec0 tables:
```sql
CREATE TRIGGER IF NOT EXISTS messages_vec_delete
    AFTER DELETE ON messages BEGIN
        DELETE FROM messages_vec WHERE rowid = old.id;
    END;

CREATE TRIGGER IF NOT EXISTS tasks_vec_delete
    AFTER DELETE ON tasks BEGIN
        DELETE FROM tasks_vec WHERE rowid = old.rowid;
    END;
```
For `chunks_vec`, the `message_chunks` table has `ON DELETE CASCADE` from messages, but that cascade won't trigger a cleanup of `chunks_vec`. Add:
```sql
CREATE TRIGGER IF NOT EXISTS chunks_vec_delete
    AFTER DELETE ON message_chunks BEGIN
        DELETE FROM chunks_vec WHERE rowid = old.id;
    END;
```
Note: These triggers need to be guarded (only created if vec0 tables exist) since the triggers would fail if sqlite-vec isn't loaded.

### 3. No FTS5 query sanitization - special characters crash search

**Where:** `switchboard/db/search.py:793` (search_messages_fts), `switchboard/db/search.py:840` (search_tasks_fts)

**Problem:** The raw user query is passed directly to FTS5 MATCH:
```python
conditions = ["messages_fts MATCH ?"]
params: list = [query]
```
FTS5 MATCH has its own query syntax with reserved operators: `"`, `*`, `(`, `)`, `AND`, `OR`, `NOT`, `NEAR`, `+`, `-`. A user searching for `"hello` (unmatched quote), `foo AND` (trailing operator), or `C++ (advanced)` will trigger `sqlite3.OperationalError: fts5: syntax error`.

No try/except wraps these queries. The error propagates up through `asyncio.gather` and crashes the entire search.

**Fix:** Either:
- (a) Wrap each FTS query term in double-quotes to treat it as a literal phrase: `'"' + query.replace('"', '""') + '"'`, OR
- (b) Wrap the FTS search calls in try/except and return `[]` on FTS syntax errors, OR
- (c) Strip/escape FTS5 operators before passing to MATCH.

Option (b) is most pragmatic: FTS errors silently degrade to vec-only results.

---

## Important

### 4. vec0 backfill loads all embeddings into memory at once

**Where:** `switchboard/server/app.py:288-302`

**Problem:** The backfill function does:
```python
msg_rows = await conn.execute_fetchall(
    "SELECT id, embedding FROM messages WHERE embedding IS NOT NULL"
)
```
This loads every message's embedding blob into memory. Each blob is 6144 bytes (1536 floats x 4 bytes). For 100K messages with embeddings, that's ~600MB of memory just for the blobs, plus Python object overhead.

The same pattern repeats for tasks (line 307) and chunks (line 326).

**Fix:** Batch with `LIMIT/OFFSET` or use a cursor:
```python
batch_size = 1000
offset = 0
while True:
    rows = await conn.execute_fetchall(
        "SELECT id, embedding FROM messages WHERE embedding IS NOT NULL LIMIT ? OFFSET ?",
        (batch_size, offset),
    )
    if not rows:
        break
    for row in rows:
        # ... insert into vec0
    await conn.commit()
    offset += batch_size
```

### 5. FTS5 update triggers fire on every column update, not just content/goal

**Where:** `switchboard/db/schema.py:625-628` (messages_fts_update), `switchboard/db/schema.py:641-644` (tasks_fts_update)

**Problem:** The `AFTER UPDATE ON messages` trigger fires whenever ANY column is updated (pinned, type, embedding, etc.), not just when `content` changes. Each trigger execution does a delete+insert into the FTS index, which is unnecessary work.

**Impact:** Low in practice since message content is rarely updated after creation, but it's wasteful when updating pinned status or embedding blobs.

**Fix:** Add a `WHEN` clause:
```sql
CREATE TRIGGER IF NOT EXISTS messages_fts_update
    AFTER UPDATE OF content ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
        INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
    END;
```

### 6. FTS5 triggers insert NULL content into FTS index

**Where:** `switchboard/db/schema.py:616-617`

**Problem:** The trigger does `INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content)`. If `new.content` is NULL, FTS5 indexes it as an empty string. The corresponding delete trigger uses `old.content`, which would also be NULL. FTS5 handles NULL→empty string consistently, so this won't cause data corruption, but it adds empty entries to the index.

More importantly, the task insert has `goal TEXT NOT NULL` so tasks_fts_insert is safe, but messages can have NULL content (the schema allows it).

**Impact:** Minor. Empty FTS entries don't match any MATCH query, so they're harmless noise.

**Fix:** Add `WHEN new.content IS NOT NULL` to the insert trigger. For the delete trigger, only delete when `old.content IS NOT NULL`.

### 7. vec0 write path is not in the same transaction as the message write

**Where:** `switchboard/db/tasks.py:425-441` (set_message_embedding), `switchboard/db/search.py:643-664` (set_task_embedding)

**Problem:** `set_message_embedding` updates the `messages` table, then does a best-effort `INSERT OR REPLACE INTO messages_vec`. Both are within the same `async with get_db()` block (same connection), and `commit()` is called once at the end. So they ARE in the same implicit transaction — if the vec0 insert throws (caught by except), the messages update still commits. This is the intended behavior (vec0 is best-effort).

However, there's a subtle issue: if vec0 insert fails, the embedding exists in `messages.embedding` but not in `messages_vec`. The `search_messages_semantic` function checks `len(query_vector) == _VEC_DIM` and only uses vec0 for 1536-dim vectors — so the Python cosine fallback never runs for production embeddings. The message is invisible to vector search until the next startup backfill.

**Impact:** Rare in practice (vec0 insert fails only if sqlite-vec extension has issues), but creates a silent gap in search coverage until restart.

**Fix:** Log when vec0 insert fails so operators can investigate:
```python
except Exception as e:
    logger.warning("vec0 insert failed for message %d: %s", message_id, e)
```

---

## Minor

### 8. Oversample multiplier inconsistency across search functions

**Where:** `switchboard/db/search.py:35` (messages: 15x), `switchboard/db/search.py:493` (chunks: 10x), `switchboard/db/search.py:698` (tasks: 10x)

**Problem:** `search_messages_semantic` uses `oversample = limit * 15` while chunks and tasks use `limit * 10`. The messages function also returns `results[:limit * 3]` while the others return `results[:limit]`. The high oversample for messages makes sense since more filtering is applied (conversation_id, project_id, type), but the 3x return multiplier means `_handle_search` gets up to `limit * 6` message results to merge, versus `limit` tasks and `limit` chunks. This creates asymmetric candidate pools.

**Impact:** Not a bug, but the asymmetry could be documented. The hybrid handler mitigates this since it takes its own `limit` parameter.

### 9. Duplicate type weight constants

**Where:** `switchboard/server/handlers/search.py:11-20` (_TYPE_BOOST), `switchboard/embeddings/service.py:49-59` (TYPE_WEIGHTS)

**Problem:** Two nearly-identical dicts define type weights. `_TYPE_BOOST` in search.py is missing `"answer": 1.0` (which defaults to 1.0 anyway via `.get(msg_type or "", 1.0)`). The handoff notes document this as intentional, but it's a maintenance risk — a change to one won't propagate to the other.

**Impact:** Cosmetic. The missing "answer" key has no effect since default is 1.0.

### 10. `idx_msg_content` index on messages.content is a full B-tree index

**Where:** `switchboard/db/schema.py:661`

**Problem:** `CREATE INDEX IF NOT EXISTS idx_msg_content ON messages(content)` creates a full B-tree index on the content column. Content can be very large (multi-KB markdown). This index is only useful for exact match or prefix lookups, not for LIKE '%query%' searches. Now that FTS5 is available, this index is dead weight — it consumes disk space and slows down writes but is never used for searching.

**Fix:** Remove the index:
```sql
DROP INDEX IF EXISTS idx_msg_content;
```

---

## Observations

### 11. Hybrid ranking math — verified correct

Walked through with concrete numbers. Given a task that appears in both FTS (BM25=4.0, max=5.0) and vec (similarity=0.85), created 30 days ago:

- FTS normalized: 4.0/5.0 = 0.8
- Vec: 0.85
- Hybrid base: 0.6 * 0.8 + 0.4 * 0.85 = 0.48 + 0.34 = 0.82
- Dual-match boost: 0.82 * 1.3 = 1.066
- Recency: 1.0 - (30/180 * 0.2) = 1.0 - 0.033 = 0.967
- Final: 1.066 * 0.967 = 1.031

The multiplicative combination can produce scores > 1.0, which is fine since scores are only used for relative ranking, not as probabilities.

### 12. Parallel execution — confirmed

`_handle_search` uses `asyncio.gather` with 5 concurrent coroutines in hybrid mode (`search.py:102-114`). FTS-only fallback runs 2 coroutines in parallel (`search.py:117-119`). This is correct.

### 13. FTS-only fallback when OPENAI_API_KEY is not set — works correctly

`embed_safe()` in `EmbeddingService` returns None when `_get_client()` raises `ValueError` (no API key). `_handle_search` checks `has_embeddings = query_vector is not None` and branches to FTS-only mode. Verified: no error is returned to the caller, just FTS results without vec/dual-match components.

### 14. Backfill idempotency — confirmed

`_backfill_vec_tables` uses `INSERT OR REPLACE INTO messages_vec(rowid, embedding)`. Re-running on restart replaces existing entries. `_backfill_fts_indexes` uses the FTS5 `'rebuild'` command which is explicitly designed to be idempotent. Both are safe to run on every startup.

### 15. cosine_similarity is still used

`cosine_similarity` from `service.py` is imported and used in `search.py` for the Python fallback path (non-1536-dim vectors). This is correct — it serves test environments and any future non-OpenAI embedding providers with different dimensions.

### 16. Race condition: embedding inserted but vec0 not yet populated

The write path (`set_message_embedding`) does both in the same transaction, so there's no window where one exists without the other (unless the vec0 insert fails, covered in finding #7). The startup backfill is a safety net. This is acceptable.

---

## Summary

| Severity | Count | Action needed |
|----------|-------|---------------|
| Critical | 3 | Must fix before merge |
| Important | 4 | Should fix, low risk of deferral |
| Minor | 3 | Nice to have |
| Observation | 6 | No action needed |

**Top 3 fixes by impact:**
1. Handle missing sqlite-vec gracefully in search functions (Critical #1) — prevents hard crash
2. Sanitize or catch FTS5 query syntax errors (Critical #3) — prevents crash on user input
3. Add vec0 delete triggers (Critical #2) — prevents phantom results and unbounded growth
