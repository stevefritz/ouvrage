"""Unified search handler — searches tasks, messages, and chunks in one call."""

import asyncio
import re
from datetime import datetime, timezone

import ouvrage.db as db
import ouvrage.db.search as _search_db
import ouvrage.db.search_weights as _sw_db
from ouvrage.embeddings import service as emb

# Type boosts for message relevance scoring
_TYPE_BOOST: dict[str, float] = {
    "spec": 1.5,
    "review": 1.4,
    "note": 1.2,
    "result": 1.1,
    "plan": 1.1,
    "question": 0.8,
    "status": 0.5,
    "test-result": 0.3,
}

_PINNED_BOOST = 1.3
_DUAL_MATCH_BOOST = 1.3

# Weights for hybrid scoring
_MSG_FTS_WEIGHT = 0.4   # messages: semantic matters more
_MSG_VEC_WEIGHT = 0.6
_TASK_FTS_WEIGHT = 0.6  # tasks: keyword precision matters more
_TASK_VEC_WEIGHT = 0.4

# Recency decay: 1.0 today → 0.3 at 3 months
_RECENCY_MAX_DAYS = 90
_RECENCY_DECAY = 0.7  # total decay over _RECENCY_MAX_DAYS


def _strip_markdown(text: str) -> str:
    """Strip markdown formatting to plain text."""
    # Remove fenced code blocks (``` ... ```)
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Remove inline code
    text = re.sub(r'`[^`\n]+`', '', text)
    # Remove headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove bold/italic (**, __, *, _)
    text = re.sub(r'\*{1,3}([^*\n]*)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}([^_\n]*)_{1,3}', r'\1', text)
    # Remove links [text](url)
    text = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', text)
    # Remove horizontal rules
    text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
    # Collapse whitespace/newlines to single spaces
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _make_search_snippet(content: str, max_len: int = 200) -> str:
    """Strip markdown and truncate to snippet ≤ max_len chars."""
    stripped = _strip_markdown(content or '')
    if len(stripped) > max_len:
        return stripped[:max_len] + '…'
    return stripped


def _recency_mult(created_at_iso: str | None, now: datetime) -> float:
    """Recency multiplier: 1.0 today, 0.3 at 3 months, linear between."""
    if not created_at_iso:
        return 1.0
    try:
        ts = created_at_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days_old = max(0, (now - dt).days)
        return 1.0 - (min(days_old, _RECENCY_MAX_DAYS) / _RECENCY_MAX_DAYS * _RECENCY_DECAY)
    except Exception:
        return 1.0


async def _handle_search(arguments: dict) -> dict:
    """Search across all Ouvrage content: task goals, messages, and message chunks.

    Runs FTS5 (keyword) and sqlite-vec (semantic) searches in parallel, merges results
    with weighted hybrid scoring, and applies type/pinned/recency/dual-match boosts.

    Falls back to FTS5-only if OPENAI_API_KEY is not set.

    Returns compact result cards (not full task objects).
    Each result: {type, entity_id, title, snippet, relevance_score, author, message_type, created_at}
    """
    query = arguments["query"]
    project_id = arguments.get("project_id")
    limit = min(int(arguments.get("limit", 10)), 30)
    now = datetime.now(timezone.utc)

    # Try to embed the query — falls back to FTS-only if embedding unavailable or vec tables missing
    service = emb.get_embedding_service()
    query_vector = await service.embed_safe(query)
    has_embeddings = query_vector is not None and _search_db.VEC_AVAILABLE

    if has_embeddings:
        # Run FTS and vec searches in parallel
        (
            fts_msg_hits,
            vec_msg_hits,
            fts_task_hits,
            vec_task_hits,
            chunk_hits,
        ) = await asyncio.gather(
            db.search_messages_fts(query, project_id=project_id, limit=limit * 2),
            db.search_messages_semantic(query_vector, project_id=project_id, limit=limit * 2),
            db.search_tasks_fts(query, project_id=project_id, limit=limit),
            db.search_tasks_semantic(query_vector, project_id=project_id, limit=limit),
            db.search_message_chunks(query_vector, project_id=project_id, limit=limit),
        )
    else:
        # Fallback: keyword-only (no OPENAI_API_KEY)
        fts_msg_hits, fts_task_hits = await asyncio.gather(
            db.search_messages_fts(query, project_id=project_id, limit=limit * 2),
            db.search_tasks_fts(query, project_id=project_id, limit=limit),
        )
        vec_msg_hits = []
        vec_task_hits = []
        chunk_hits = []

    # --- Normalize FTS BM25 scores to 0-1 ---
    raw_fts_msg = {r["message_id"]: r["bm25_score"] for r in fts_msg_hits}
    raw_fts_task = {r["task_id"]: r["bm25_score"] for r in fts_task_hits}

    fts_msg_max = max(raw_fts_msg.values(), default=1.0) or 1.0
    fts_task_max = max(raw_fts_task.values(), default=1.0) or 1.0
    fts_msg_norm: dict[int, float] = {k: v / fts_msg_max for k, v in raw_fts_msg.items()}
    fts_task_norm: dict[str, float] = {k: v / fts_task_max for k, v in raw_fts_task.items()}

    # Vec similarity scores are already 0-1
    vec_msg_norm: dict[int, float] = {r["message_id"]: r["similarity"] for r in vec_msg_hits}
    vec_task_norm: dict[str, float] = {r["task_id"]: r["similarity"] for r in vec_task_hits}

    # --- Build metadata lookup maps ---
    # For messages: start with FTS (has less fields), override with vec (more complete)
    msg_meta: dict[int, dict] = {}
    for r in fts_msg_hits:
        msg_meta[r["message_id"]] = {
            "type": r.get("type"),
            "author": r.get("author"),
            "task_id": r.get("task_id"),
            "conversation_id": r.get("conversation_id"),
            "created_at": r.get("created_at"),
            "pinned": False,
            "title": None,
            "content": None,
            "fts_snippet": r.get("snippet"),
        }
    for r in vec_msg_hits:
        # Vec metadata overrides FTS — it includes pinned, title, content
        msg_meta[r["message_id"]] = {
            "type": r.get("type"),
            "author": r.get("author"),
            "task_id": r.get("task_id"),
            "conversation_id": r.get("conversation_id"),
            "created_at": r.get("created_at"),
            "pinned": r.get("pinned", False),
            "title": r.get("title"),
            "content": r.get("content"),
            "fts_snippet": msg_meta.get(r["message_id"], {}).get("fts_snippet"),
        }

    # For tasks: both FTS and vec have the same fields; vec preferred
    task_meta: dict[str, dict] = {}
    for r in fts_task_hits:
        task_meta[r["task_id"]] = {
            "goal": r.get("goal"),
            "status": r.get("status"),
            "created_at": r.get("created_at"),
        }
    for r in vec_task_hits:
        task_meta[r["task_id"]] = {
            "goal": r.get("goal"),
            "status": r.get("status"),
            "created_at": r.get("created_at"),
        }

    # --- Build task candidates with hybrid scores ---
    task_ids = set(fts_task_norm) | set(vec_task_norm)
    task_candidates = []
    for task_id in task_ids:
        fts_s = fts_task_norm.get(task_id, 0.0)
        vec_s = vec_task_norm.get(task_id, 0.0)
        base = _TASK_FTS_WEIGHT * fts_s + _TASK_VEC_WEIGHT * vec_s

        meta = task_meta.get(task_id, {})
        dual_mult = _DUAL_MATCH_BOOST if (task_id in fts_task_norm and task_id in vec_task_norm) else 1.0
        rec_mult = _recency_mult(meta.get("created_at"), now)

        final_score = base * dual_mult * rec_mult

        goal = meta.get("goal") or ""
        task_candidates.append({
            "type": "task",
            "entity_id": task_id,
            "task_id": task_id,
            "conversation_id": None,
            "title": goal,
            "snippet": _make_search_snippet(goal),
            "relevance_score": round(final_score, 4),
            "author": None,
            "message_type": None,
            "created_at": meta.get("created_at"),
            "status": meta.get("status"),
        })

    # --- Build message candidates (skip those covered by chunk hits) ---
    chunk_message_ids = {hit["message_id"] for hit in chunk_hits}

    msg_ids = set(fts_msg_norm) | set(vec_msg_norm)
    msg_candidates = []
    for msg_id in msg_ids:
        if msg_id in chunk_message_ids:
            continue  # chunk hit takes precedence

        fts_s = fts_msg_norm.get(msg_id, 0.0)
        vec_s = vec_msg_norm.get(msg_id, 0.0)
        base = _MSG_FTS_WEIGHT * fts_s + _MSG_VEC_WEIGHT * vec_s

        meta = msg_meta.get(msg_id, {})
        msg_type = meta.get("type")
        type_mult = _TYPE_BOOST.get(msg_type or "", 1.0)
        pinned_mult = _PINNED_BOOST if meta.get("pinned") else 1.0
        dual_mult = _DUAL_MATCH_BOOST if (msg_id in fts_msg_norm and msg_id in vec_msg_norm) else 1.0
        rec_mult = _recency_mult(meta.get("created_at"), now)

        final_score = base * type_mult * pinned_mult * dual_mult * rec_mult

        # Prefer rich content snippet; fall back to FTS snippet
        content = meta.get("content")
        snippet = _make_search_snippet(content) if content else (meta.get("fts_snippet") or "")

        result_type = "task_message" if meta.get("task_id") else "conversation_message"
        msg_candidates.append({
            "type": result_type,
            "entity_id": str(msg_id),
            "task_id": meta.get("task_id"),
            "conversation_id": meta.get("conversation_id"),
            "title": meta.get("title"),
            "snippet": snippet,
            "relevance_score": round(final_score, 4),
            "author": meta.get("author"),
            "message_type": msg_type,
            "created_at": meta.get("created_at"),
        })

    # --- Build chunk candidates ---
    chunk_candidates = []
    for hit in chunk_hits:
        base = hit["similarity"]
        msg_type = hit.get("type")
        type_mult = _TYPE_BOOST.get(msg_type or "", 1.0)
        pinned_mult = _PINNED_BOOST if hit.get("pinned") else 1.0
        rec_mult = _recency_mult(hit.get("created_at"), now)
        # No dual-match boost for chunks (no FTS chunk search exists)

        final_score = base * type_mult * pinned_mult * rec_mult

        result_type = "task_message" if hit.get("task_id") else "conversation_message"
        title = hit.get("title") or hit.get("chunk_heading")
        chunk_candidates.append({
            "type": "chunk",
            "entity_id": str(hit["message_id"]),
            "task_id": hit.get("task_id"),
            "conversation_id": hit.get("conversation_id"),
            "title": title,
            "snippet": _make_search_snippet(hit.get("chunk_content") or ""),
            "relevance_score": round(final_score, 4),
            "author": hit.get("author"),
            "message_type": msg_type,
            "created_at": hit.get("created_at"),
        })

    # --- Merge all candidates, deduplicate by entity_id (keep highest score) ---
    all_candidates = task_candidates + msg_candidates + chunk_candidates

    best: dict[str, dict] = {}
    for c in all_candidates:
        eid = c["entity_id"]
        if eid not in best or c["relevance_score"] > best[eid]["relevance_score"]:
            best[eid] = c

    candidates = sorted(best.values(), key=lambda r: r["relevance_score"], reverse=True)
    results = candidates[:limit]

    return {"results": results, "total_candidates": len(candidates)}


async def _handle_set_weight(arguments: dict) -> dict:
    """Set a search ranking weight for a specific entity.

    Upserts (entity_type, entity_id) → weight. Returns the resulting row.
    Raises ValueError on invalid entity_type or out-of-range weight.
    """
    entity_type = arguments["entity_type"]
    entity_id = str(arguments["entity_id"])
    weight = float(arguments["weight"])
    reason = arguments.get("reason")

    try:
        row = await _sw_db.set_weight(
            entity_type=entity_type,
            entity_id=entity_id,
            weight=weight,
            reason=reason,
        )
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    return row
