"""Semantic search, full-text search, activity feeds, component search, and chunk indexing."""
import json
import logging
import httpx

from ouvrage.db.connection import get_db
from ouvrage.db._helpers import _make_snippet
from ouvrage.embeddings.chunks import MIN_CHUNK_LENGTH, chunk_message

log = logging.getLogger(__name__)

# Expected embedding dimension for vec0 tables
_VEC_DIM = 1536

# Module-level flag: True when sqlite-vec is loaded and vec0 tables are queryable.
# Set by _check_vec_tables() at startup. Default False prevents crashes when
# sqlite-vec extension is missing but OPENAI_API_KEY is set.
VEC_AVAILABLE = False


async def _check_vec_tables() -> None:
    """Check if vec0 tables are queryable and set VEC_AVAILABLE accordingly.

    Called at app startup. Sets the module-level VEC_AVAILABLE flag so search
    handlers can skip vec queries when sqlite-vec is not loaded.
    """
    global VEC_AVAILABLE
    try:
        async with get_db() as db:
            await db.execute_fetchall("SELECT count(*) FROM messages_vec LIMIT 1")
        VEC_AVAILABLE = True
    except Exception:
        VEC_AVAILABLE = False
        log.warning("sqlite-vec tables not available — vector search degraded to FTS-only")


async def search_messages_semantic(
    query_vector: list[float],
    conversation_id: str | None = None,
    project_id: str | None = None,
    type_filter: list[str] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search messages by vector similarity.

    Uses sqlite-vec indexed queries for standard 1536-dim OpenAI embeddings.
    Falls back to Python cosine loop for non-standard dimensions (e.g. in tests).

    Filtering by project_id joins through the conversations table.
    Actual relevance scoring (type weights, pinned boost) is applied by the caller.
    Returns up to limit*3 results so caller has room to re-rank.
    """
    from ouvrage.embeddings.service import encode_vector, decode_vector, cosine_similarity

    # Only use vec0 for the expected 1536-dim vectors
    if len(query_vector) == _VEC_DIM:
        blob = encode_vector(query_vector)
        oversample = limit * 15  # Oversample to account for filtering

        try:
            async with get_db() as db:
                vec_rows = await db.execute_fetchall(
                    "SELECT rowid, distance FROM messages_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (blob, oversample),
                )
        except Exception as exc:
            log.error("messages_vec query failed, returning empty results: %s", exc)
            return []

        if not vec_rows:
            return []

        rowids = [r["rowid"] for r in vec_rows]
        distance_map = {r["rowid"]: r["distance"] for r in vec_rows}

        id_placeholders = ",".join("?" * len(rowids))
        conditions = [f"m.id IN ({id_placeholders})"]
        params: list = list(rowids)

        if conversation_id:
            conditions.append("m.conversation_id = ?")
            params.append(conversation_id)

        if project_id:
            conditions.append(
                "(m.conversation_id IN (SELECT id FROM conversations WHERE project = ?) "
                "OR m.task_id IN (SELECT id FROM tasks WHERE project_id = ?))"
            )
            params.extend([project_id, project_id])

        if type_filter:
            type_placeholders = ",".join("?" * len(type_filter))
            conditions.append(f"m.type IN ({type_placeholders})")
            params.extend(type_filter)

        where = " AND ".join(conditions)
        async with get_db() as db:
            rows = await db.execute_fetchall(
                f"""SELECT m.id, m.conversation_id, m.task_id, m.author, m.type, m.title,
                           m.content, m.pinned, m.created_at
                    FROM messages m WHERE {where}""",
                params,
            )

        results = []
        for row in rows:
            distance = distance_map[row["id"]]
            # OpenAI embeddings are normalized: L2 dist = 2 - 2*cosine → similarity = 1 - dist/2
            similarity = max(0.0, 1.0 - (distance / 2.0))
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
                "similarity": similarity,
            })

        results.sort(key=lambda r: r["similarity"], reverse=True)
        return results[:limit * 3]

    # Fallback: Python cosine loop for non-1536-dim vectors
    async with get_db() as db:
        conditions = ["m.embedding IS NOT NULL"]
        params_fb: list = []

        if conversation_id:
            conditions.append("m.conversation_id = ?")
            params_fb.append(conversation_id)

        if project_id:
            conditions.append(
                "(m.conversation_id IN (SELECT id FROM conversations WHERE project = ?) "
                "OR m.task_id IN (SELECT id FROM tasks WHERE project_id = ?))"
            )
            params_fb.extend([project_id, project_id])

        if type_filter:
            placeholders = ",".join("?" * len(type_filter))
            conditions.append(f"m.type IN ({placeholders})")
            params_fb.extend(type_filter)

        where = " AND ".join(conditions)
        rows = await db.execute_fetchall(
            f"""SELECT m.id, m.conversation_id, m.task_id, m.author, m.type, m.title,
                       m.content, m.pinned, m.created_at, m.embedding
                FROM messages m WHERE {where}""",
            params_fb,
        )

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

    results.sort(key=lambda r: r["similarity"], reverse=True)
    return results[:limit * 3]


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


async def index_message_chunks(message_id: int, content: str) -> None:
    """Chunk a message and embed each chunk. Idempotent — deletes existing chunks first.

    If the message doesn't produce chunks (too short, no headers, single section),
    inserts a sentinel row (chunk_index=-1) so get_messages_needing_chunking() skips it.
    """
    chunks = chunk_message(content)

    async with get_db() as db:
        await db.execute("DELETE FROM message_chunks WHERE message_id = ?", (message_id,))

        if not chunks:
            # Insert sentinel so get_messages_needing_chunking() skips this message
            await db.execute(
                """INSERT INTO message_chunks (message_id, chunk_index, heading, content, embedding)
                   VALUES (?, -1, NULL, '', NULL)""",
                (message_id,),
            )
            await db.commit()
            return

        from ouvrage.embeddings.service import get_embedding_service, encode_vector

        service = get_embedding_service()

        for chunk in chunks:
            vector = await service.embed_safe(chunk["content"])
            blob = encode_vector(vector) if vector else None
            cursor = await db.execute(
                """INSERT INTO message_chunks (message_id, chunk_index, heading, content, embedding)
                   VALUES (?, ?, ?, ?, ?)""",
                (message_id, chunk["chunk_index"], chunk["heading"], chunk["content"], blob),
            )
            # Also insert into chunks_vec — only for standard 1536-dim embeddings
            if blob and cursor.lastrowid and len(blob) == 1536 * 4:
                try:
                    await db.execute(
                        "INSERT OR REPLACE INTO chunks_vec(rowid, embedding) VALUES (?, ?)",
                        (cursor.lastrowid, blob),
                    )
                except Exception as e:
                    log.warning("vec0 insert failed for chunk rowid %d: %s", cursor.lastrowid, e)
        await db.commit()


async def search_message_chunks(
    query_vector: list[float],
    conversation_id: str | None = None,
    project_id: str | None = None,
    type_filter: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Search message chunks by vector similarity, returning hits with adjacent context.

    Uses sqlite-vec indexed queries for standard 1536-dim OpenAI embeddings.
    Falls back to Python cosine loop for non-standard dimensions (e.g. in tests).
    """
    from ouvrage.embeddings.service import encode_vector, decode_vector, cosine_similarity

    def _build_chunk_result(row, similarity):
        return {
            "chunk_id": row["id"],
            "message_id": row["message_id"],
            "chunk_index": row["chunk_index"],
            "chunk_heading": row["heading"],
            "chunk_content": row["content"],
            "conversation_id": row["conversation_id"],
            "task_id": row["task_id"],
            "author": row["author"],
            "type": row["type"],
            "title": row["title"],
            "pinned": bool(row["pinned"]),
            "created_at": row["created_at"],
            "similarity": similarity,
        }

    if len(query_vector) == _VEC_DIM:
        blob = encode_vector(query_vector)
        oversample = limit * 10

        try:
            async with get_db() as db:
                vec_rows = await db.execute_fetchall(
                    "SELECT rowid, distance FROM chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (blob, oversample),
                )
        except Exception as exc:
            log.error("chunks_vec query failed, returning empty results: %s", exc)
            return []

        if not vec_rows:
            return []

        rowids = [r["rowid"] for r in vec_rows]
        distance_map = {r["rowid"]: r["distance"] for r in vec_rows}

        id_placeholders = ",".join("?" * len(rowids))
        conditions = [f"mc.id IN ({id_placeholders})", "mc.chunk_index >= 0"]
        params: list = list(rowids)

        if conversation_id:
            conditions.append("m.conversation_id = ?")
            params.append(conversation_id)

        if project_id:
            conditions.append(
                "(m.conversation_id IN (SELECT id FROM conversations WHERE project = ?) "
                "OR m.task_id IN (SELECT id FROM tasks WHERE project_id = ?))"
            )
            params.extend([project_id, project_id])

        if type_filter:
            conditions.append("m.type = ?")
            params.append(type_filter)

        where = " AND ".join(conditions)
        async with get_db() as db:
            rows = await db.execute_fetchall(
                f"""SELECT mc.id, mc.message_id, mc.chunk_index, mc.heading, mc.content,
                           m.conversation_id, m.task_id, m.author, m.type, m.title, m.created_at, m.pinned
                    FROM message_chunks mc
                    JOIN messages m ON m.id = mc.message_id
                    WHERE {where}""",
                params,
            )

        scored = [
            _build_chunk_result(row, max(0.0, 1.0 - (distance_map[row["id"]] / 2.0)))
            for row in rows
        ]

    else:
        # Fallback: Python cosine loop for non-1536-dim vectors
        async with get_db() as db:
            conditions = ["mc.embedding IS NOT NULL", "mc.chunk_index >= 0"]
            params_fb: list = []

            if conversation_id:
                conditions.append("m.conversation_id = ?")
                params_fb.append(conversation_id)

            if project_id:
                conditions.append(
                    "(m.conversation_id IN (SELECT id FROM conversations WHERE project = ?) "
                    "OR m.task_id IN (SELECT id FROM tasks WHERE project_id = ?))"
                )
                params_fb.extend([project_id, project_id])

            if type_filter:
                conditions.append("m.type = ?")
                params_fb.append(type_filter)

            where = " AND ".join(conditions)
            rows = await db.execute_fetchall(
                f"""SELECT mc.id, mc.message_id, mc.chunk_index, mc.heading, mc.content, mc.embedding,
                           m.conversation_id, m.task_id, m.author, m.type, m.title, m.created_at, m.pinned
                    FROM message_chunks mc
                    JOIN messages m ON m.id = mc.message_id
                    WHERE {where}""",
                params_fb,
            )

        scored = []
        for row in rows:
            blob = row["embedding"]
            if not blob:
                continue
            try:
                vec = decode_vector(blob)
            except Exception:
                continue
            sim = cosine_similarity(query_vector, vec)
            scored.append(_build_chunk_result(row, sim))

    scored.sort(key=lambda r: r["similarity"], reverse=True)
    top = scored[:limit]

    # Fetch adjacent chunks (±1) for context window
    if top:
        async with get_db() as db:
            for hit in top:
                adj_rows = await db.execute_fetchall(
                    """SELECT chunk_index, heading, content FROM message_chunks
                       WHERE message_id = ? AND chunk_index IN (?, ?)
                       ORDER BY chunk_index""",
                    (hit["message_id"], hit["chunk_index"] - 1, hit["chunk_index"] + 1),
                )
                hit["context_chunks"] = [
                    {"chunk_index": r["chunk_index"], "heading": r["heading"], "content": r["content"]}
                    for r in adj_rows
                ]

    return top


async def search_conversation_messages(
    conversation_id: str,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """LIKE search on messages in a specific conversation.

    Returns message objects with id, author, type, title, snippet (~200 chars),
    score (1.0 for LIKE matches), and created_at.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT id, author, type, title, content, created_at
               FROM messages
               WHERE conversation_id = ? AND content LIKE ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (conversation_id, f"%{query}%", limit),
        )
        results = []
        for r in rows:
            row = dict(r)
            content = row.pop("content", "") or ""
            lower_content = content.lower()
            idx = lower_content.find(query.lower())
            if idx >= 0:
                start = max(0, idx - 90)
                end = min(len(content), idx + len(query) + 90)
                snippet = ("..." if start > 0 else "") + content[start:end] + ("..." if end < len(content) else "")
            else:
                snippet = content[:200] + ("..." if len(content) > 200 else "")
            row["snippet"] = snippet
            row["score"] = 1.0
            results.append(row)
        return results


async def set_task_embedding(task_id: str, blob: bytes) -> None:
    """Store the embedding blob for a task's goal and update tasks_vec."""
    async with get_db() as db:
        await db.execute(
            "UPDATE tasks SET embedding = ? WHERE id = ?",
            (blob, task_id),
        )
        # Keep tasks_vec in sync — only for standard 1536-dim embeddings (6144 bytes)
        # tasks.id is TEXT; use the INTEGER rowid to key the vec0 table
        if len(blob) == 1536 * 4:
            try:
                rows = await db.execute_fetchall(
                    "SELECT rowid FROM tasks WHERE id = ?", (task_id,)
                )
                if rows:
                    await db.execute(
                        "INSERT OR REPLACE INTO tasks_vec(rowid, embedding) VALUES (?, ?)",
                        (rows[0]["rowid"], blob),
                    )
            except Exception as e:
                log.warning("vec0 insert failed for task %s: %s", task_id, e)
        await db.commit()


async def get_tasks_needing_embedding(batch_size: int = 100) -> list[dict]:
    """Return tasks with no embedding whose goal is non-empty."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT id, project_id, goal
               FROM tasks
               WHERE embedding IS NULL
                 AND goal IS NOT NULL
                 AND goal != ''
               ORDER BY created_at ASC
               LIMIT ?""",
            (batch_size,),
        )
        return [dict(r) for r in rows]


async def search_tasks_semantic(
    query_vector: list[float],
    project_id: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Vector similarity search across task goals.

    Uses sqlite-vec indexed queries for standard 1536-dim OpenAI embeddings.
    Falls back to Python cosine loop for non-standard dimensions (e.g. in tests).
    """
    from ouvrage.embeddings.service import encode_vector, decode_vector, cosine_similarity

    if len(query_vector) == _VEC_DIM:
        blob = encode_vector(query_vector)
        oversample = limit * 10

        try:
            async with get_db() as db:
                vec_rows = await db.execute_fetchall(
                    "SELECT rowid, distance FROM tasks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (blob, oversample),
                )
        except Exception as exc:
            log.error("tasks_vec query failed, returning empty results: %s", exc)
            return []

        if not vec_rows:
            return []

        rowids = [r["rowid"] for r in vec_rows]
        distance_map = {r["rowid"]: r["distance"] for r in vec_rows}

        rowid_placeholders = ",".join("?" * len(rowids))
        conditions = [f"rowid IN ({rowid_placeholders})"]
        params: list = list(rowids)

        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)

        where = " AND ".join(conditions)

        async with get_db() as db:
            rows = await db.execute_fetchall(
                f"SELECT rowid, id, project_id, goal, status, created_at FROM tasks WHERE {where}",
                params,
            )

        results = []
        for row in rows:
            distance = distance_map[row["rowid"]]
            similarity = max(0.0, 1.0 - (distance / 2.0))
            results.append({
                "task_id": row["id"],
                "project_id": row["project_id"],
                "goal": row["goal"],
                "status": row["status"],
                "created_at": row["created_at"],
                "similarity": similarity,
            })

        results.sort(key=lambda r: r["similarity"], reverse=True)
        return results[:limit]

    # Fallback: Python cosine loop for non-1536-dim vectors
    async with get_db() as db:
        conditions = ["embedding IS NOT NULL"]
        params_fb: list = []

        if project_id:
            conditions.append("project_id = ?")
            params_fb.append(project_id)

        where = " AND ".join(conditions)
        rows = await db.execute_fetchall(
            f"SELECT id, project_id, goal, status, created_at, embedding FROM tasks WHERE {where}",
            params_fb,
        )

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
            "task_id": row["id"],
            "project_id": row["project_id"],
            "goal": row["goal"],
            "status": row["status"],
            "created_at": row["created_at"],
            "similarity": sim,
        })

    results.sort(key=lambda r: r["similarity"], reverse=True)
    return results[:limit]


def sanitize_fts_query(query: str) -> str | None:
    """Wrap each word in double quotes for literal FTS5 matching.

    Prevents special characters (*, +, -, (, ), AND, OR, NOT, NEAR, double-quote) from
    being interpreted as FTS5 operators. Internal double quotes are escaped as two double quotes.

    Examples::

        C++ (advanced)  -> "C++" "(advanced)"
        AND OR NOT       -> "AND" "OR" "NOT"

    Returns None if query has no words.
    """
    words = query.split()
    if not words:
        return None
    return ' '.join('"' + w.replace('"', '""') + '"' for w in words)


async def search_messages_fts(
    query: str,
    conversation_id: str | None = None,
    project_id: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """FTS5 full-text search over messages.content with BM25 ranking.

    Returns rows with message_id, snippet (~200 chars), bm25_score, author, type,
    task_id, conversation_id, created_at.
    """
    sanitized = sanitize_fts_query(query)
    if sanitized is None:
        return []

    try:
        async with get_db() as db:
            conditions = ["messages_fts MATCH ?"]
            params: list = [sanitized]

            if conversation_id:
                conditions.append("m.conversation_id = ?")
                params.append(conversation_id)

            if project_id:
                conditions.append(
                    "(m.conversation_id IN (SELECT id FROM conversations WHERE project = ?) "
                    "OR m.task_id IN (SELECT id FROM tasks WHERE project_id = ?))"
                )
                params.extend([project_id, project_id])

            where = " AND ".join(conditions)
            params.append(limit)

            sql = f"""
                SELECT
                    m.id AS message_id,
                    snippet(messages_fts, 0, '', '', '...', 32) AS snippet,
                    -bm25(messages_fts) AS bm25_score,
                    m.author,
                    m.type,
                    m.task_id,
                    m.conversation_id,
                    m.created_at
                FROM messages_fts
                JOIN messages m ON m.id = messages_fts.rowid
                WHERE {where}
                ORDER BY bm25(messages_fts)
                LIMIT ?
            """
            rows = await db.execute_fetchall(sql, params)
            return [dict(r) for r in rows]
    except Exception as exc:
        log.error("messages_fts search failed, returning empty results: %s", exc)
        return []


async def search_tasks_fts(
    query: str,
    project_id: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """FTS5 full-text search over tasks.goal with BM25 ranking.

    Returns rows with task_id, goal, bm25_score, status, created_at.
    """
    sanitized = sanitize_fts_query(query)
    if sanitized is None:
        return []

    try:
        async with get_db() as db:
            conditions = ["tasks_fts MATCH ?"]
            params: list = [sanitized]

            if project_id:
                conditions.append("t.project_id = ?")
                params.append(project_id)

            where = " AND ".join(conditions)
            params.append(limit)

            sql = f"""
                SELECT
                    t.id AS task_id,
                    t.goal,
                    -bm25(tasks_fts) AS bm25_score,
                    t.status,
                    t.created_at
                FROM tasks_fts
                JOIN tasks t ON t.rowid = tasks_fts.rowid
                WHERE {where}
                ORDER BY bm25(tasks_fts)
                LIMIT ?
            """
            rows = await db.execute_fetchall(sql, params)
            return [dict(r) for r in rows]
    except Exception as exc:
        log.error("tasks_fts search failed, returning empty results: %s", exc)
        return []


async def get_messages_needing_chunking(batch_size: int = 100) -> list[dict]:
    """Return messages >= 500 chars that haven't been chunked yet (no entry in message_chunks)."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT m.id, m.content
               FROM messages m
               WHERE length(m.content) >= ?
                 AND NOT EXISTS (SELECT 1 FROM message_chunks mc WHERE mc.message_id = m.id)
               ORDER BY m.id ASC
               LIMIT ?""",
            (MIN_CHUNK_LENGTH, batch_size),
        )
        return [dict(r) for r in rows]


async def set_file_embedding(file_id: str, blob: bytes) -> None:
    """Store the embedding blob for a file and update files_vec."""
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO files_embeddings (file_id, embedding) VALUES (?, ?)",
            (file_id, blob),
        )
        # Keep files_vec in sync — only for standard 1536-dim embeddings (6144 bytes)
        # files.id is TEXT; use the INTEGER rowid to key the vec0 table
        if len(blob) == 1536 * 4:
            try:
                rows = await db.execute_fetchall(
                    "SELECT rowid FROM files WHERE id = ?", (file_id,)
                )
                if rows:
                    await db.execute(
                        "INSERT OR REPLACE INTO files_vec(rowid, embedding) VALUES (?, ?)",
                        (rows[0]["rowid"], blob),
                    )
            except Exception as e:
                log.warning("vec0 insert failed for file %s: %s", file_id, e)
        await db.commit()


async def index_doc_file(file_id: str) -> None:
    """Chunk a reference doc file and embed each chunk. Idempotent — deletes existing chunks first.

    Only operates on files with role='reference_doc'. Returns silently otherwise.
    If the file doesn't produce chunks (too short, no headers, single section),
    inserts a sentinel row (chunk_index=-1) so get_doc_files_needing_chunking() skips it.
    Mirrors index_message_chunks in structure.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT id, stored_path, role FROM files WHERE id = ?",
            (file_id,),
        )
    if not rows or rows[0]["role"] != "reference_doc":
        return

    stored_path = rows[0]["stored_path"]
    try:
        with open(stored_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, IOError) as e:
        log.warning("index_doc_file: could not read %s: %s", stored_path, e)
        return

    chunks = chunk_message(content)

    from ouvrage.embeddings.service import get_embedding_service, encode_vector

    service = get_embedding_service()

    async with get_db() as db:
        await db.execute("DELETE FROM file_chunks WHERE file_id = ?", (file_id,))

        if not chunks:
            # Insert sentinel so get_doc_files_needing_chunking() skips this file
            await db.execute(
                """INSERT INTO file_chunks (file_id, chunk_index, heading, content, embedding)
                   VALUES (?, -1, NULL, '', NULL)""",
                (file_id,),
            )
            await db.commit()
        else:
            for chunk in chunks:
                vector = await service.embed_safe(chunk["content"])
                blob = encode_vector(vector) if vector else None
                cursor = await db.execute(
                    """INSERT INTO file_chunks (file_id, chunk_index, heading, content, embedding)
                       VALUES (?, ?, ?, ?, ?)""",
                    (file_id, chunk["chunk_index"], chunk["heading"], chunk["content"], blob),
                )
                # Also insert into file_chunks_vec — only for standard 1536-dim embeddings
                if blob and cursor.lastrowid and len(blob) == 1536 * 4:
                    try:
                        await db.execute(
                            "INSERT OR REPLACE INTO file_chunks_vec(rowid, embedding) VALUES (?, ?)",
                            (cursor.lastrowid, blob),
                        )
                    except Exception as e:
                        log.warning("vec0 insert failed for file chunk rowid %d: %s", cursor.lastrowid, e)
            await db.commit()

    # Embed whole-file content (service truncates to ~32KB internally)
    whole_vector = await service.embed_safe(content)
    if whole_vector:
        whole_blob = encode_vector(whole_vector)
        await set_file_embedding(file_id, whole_blob)


async def get_doc_files_needing_chunking(batch_size: int = 100) -> list[str]:
    """Return file_ids for reference_doc files that haven't been chunked yet."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT f.id
               FROM files f
               WHERE f.role = 'reference_doc'
                 AND NOT EXISTS (SELECT 1 FROM file_chunks fc WHERE fc.file_id = f.id)
               ORDER BY f.id ASC
               LIMIT ?""",
            (batch_size,),
        )
        return [r["id"] for r in rows]


async def search_files_semantic(
    query_vector: list[float],
    project_id: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search reference doc files by vector similarity.

    Uses sqlite-vec indexed queries for standard 1536-dim OpenAI embeddings.
    Falls back to Python cosine loop for non-standard dimensions (e.g. in tests).
    Filters to role='reference_doc' files only.
    """
    from ouvrage.embeddings.service import encode_vector, decode_vector, cosine_similarity

    if len(query_vector) == _VEC_DIM:
        blob = encode_vector(query_vector)
        oversample = limit * 15

        try:
            async with get_db() as db:
                vec_rows = await db.execute_fetchall(
                    "SELECT rowid, distance FROM files_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (blob, oversample),
                )
        except Exception as exc:
            log.error("files_vec query failed, returning empty results: %s", exc)
            return []

        if not vec_rows:
            return []

        rowids = [r["rowid"] for r in vec_rows]
        distance_map = {r["rowid"]: r["distance"] for r in vec_rows}

        rowid_placeholders = ",".join("?" * len(rowids))
        conditions = [f"rowid IN ({rowid_placeholders})", "role = 'reference_doc'"]
        params: list = list(rowids)

        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)

        where = " AND ".join(conditions)
        async with get_db() as db:
            rows = await db.execute_fetchall(
                f"SELECT rowid, id, filename, project_id, created_at FROM files WHERE {where}",
                params,
            )

        results = []
        for row in rows:
            distance = distance_map[row["rowid"]]
            similarity = max(0.0, 1.0 - (distance / 2.0))
            results.append({
                "file_id": row["id"],
                "filename": row["filename"],
                "project_id": row["project_id"],
                "created_at": row["created_at"],
                "similarity": similarity,
            })

        results.sort(key=lambda r: r["similarity"], reverse=True)
        return results[:limit]

    # Fallback: Python cosine loop for non-1536-dim vectors
    async with get_db() as db:
        conditions = ["fe.embedding IS NOT NULL", "f.role = 'reference_doc'"]
        params_fb: list = []

        if project_id:
            conditions.append("f.project_id = ?")
            params_fb.append(project_id)

        where = " AND ".join(conditions)
        rows = await db.execute_fetchall(
            f"""SELECT f.id, f.filename, f.project_id, f.created_at, fe.embedding
                FROM files f
                JOIN files_embeddings fe ON fe.file_id = f.id
                WHERE {where}""",
            params_fb,
        )

    results = []
    for row in rows:
        emb_blob = row["embedding"]
        if not emb_blob:
            continue
        try:
            vec = decode_vector(emb_blob)
        except Exception:
            continue
        sim = cosine_similarity(query_vector, vec)
        results.append({
            "file_id": row["id"],
            "filename": row["filename"],
            "project_id": row["project_id"],
            "created_at": row["created_at"],
            "similarity": sim,
        })

    results.sort(key=lambda r: r["similarity"], reverse=True)
    return results[:limit]


async def search_file_chunks_semantic(
    query_vector: list[float],
    project_id: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Search file chunks by vector similarity, returning hits with adjacent context.

    Uses sqlite-vec indexed queries for standard 1536-dim OpenAI embeddings.
    Falls back to Python cosine loop for non-standard dimensions (e.g. in tests).
    Filters to role='reference_doc' files only.
    """
    from ouvrage.embeddings.service import encode_vector, decode_vector, cosine_similarity

    def _build_file_chunk_result(row, similarity):
        return {
            "chunk_id": row["id"],
            "file_id": row["file_id"],
            "chunk_index": row["chunk_index"],
            "chunk_heading": row["heading"],
            "chunk_content": row["content"],
            "filename": row["filename"],
            "project_id": row["project_id"],
            "created_at": row["created_at"],
            "similarity": similarity,
        }

    if len(query_vector) == _VEC_DIM:
        blob = encode_vector(query_vector)
        oversample = limit * 10

        try:
            async with get_db() as db:
                vec_rows = await db.execute_fetchall(
                    "SELECT rowid, distance FROM file_chunks_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                    (blob, oversample),
                )
        except Exception as exc:
            log.error("file_chunks_vec query failed, returning empty results: %s", exc)
            return []

        if not vec_rows:
            return []

        rowids = [r["rowid"] for r in vec_rows]
        distance_map = {r["rowid"]: r["distance"] for r in vec_rows}

        id_placeholders = ",".join("?" * len(rowids))
        conditions = [f"fc.id IN ({id_placeholders})", "fc.chunk_index >= 0", "f.role = 'reference_doc'"]
        params: list = list(rowids)

        if project_id:
            conditions.append("f.project_id = ?")
            params.append(project_id)

        where = " AND ".join(conditions)
        async with get_db() as db:
            rows = await db.execute_fetchall(
                f"""SELECT fc.id, fc.file_id, fc.chunk_index, fc.heading, fc.content,
                           f.filename, f.project_id, f.created_at
                    FROM file_chunks fc
                    JOIN files f ON f.id = fc.file_id
                    WHERE {where}""",
                params,
            )

        scored = [
            _build_file_chunk_result(row, max(0.0, 1.0 - (distance_map[row["id"]] / 2.0)))
            for row in rows
        ]

    else:
        # Fallback: Python cosine loop for non-1536-dim vectors
        async with get_db() as db:
            conditions = ["fc.embedding IS NOT NULL", "fc.chunk_index >= 0", "f.role = 'reference_doc'"]
            params_fb: list = []

            if project_id:
                conditions.append("f.project_id = ?")
                params_fb.append(project_id)

            where = " AND ".join(conditions)
            rows = await db.execute_fetchall(
                f"""SELECT fc.id, fc.file_id, fc.chunk_index, fc.heading, fc.content, fc.embedding,
                           f.filename, f.project_id, f.created_at
                    FROM file_chunks fc
                    JOIN files f ON f.id = fc.file_id
                    WHERE {where}""",
                params_fb,
            )

        scored = []
        for row in rows:
            emb_blob = row["embedding"]
            if not emb_blob:
                continue
            try:
                vec = decode_vector(emb_blob)
            except Exception:
                continue
            sim = cosine_similarity(query_vector, vec)
            scored.append(_build_file_chunk_result(row, sim))

    scored.sort(key=lambda r: r["similarity"], reverse=True)
    top = scored[:limit]

    # Fetch adjacent chunks (±1) for context window
    if top:
        async with get_db() as db:
            for hit in top:
                adj_rows = await db.execute_fetchall(
                    """SELECT chunk_index, heading, content FROM file_chunks
                       WHERE file_id = ? AND chunk_index IN (?, ?)
                       ORDER BY chunk_index""",
                    (hit["file_id"], hit["chunk_index"] - 1, hit["chunk_index"] + 1),
                )
                hit["context_chunks"] = [
                    {"chunk_index": r["chunk_index"], "heading": r["heading"], "content": r["content"]}
                    for r in adj_rows
                ]

    return top
