"""Unified search handler — searches tasks, messages, and chunks in one call."""

import asyncio
import re

import switchboard.db as db
from switchboard.embeddings import service as emb
from switchboard.embeddings.service import compute_relevance_score


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


async def _handle_search(arguments: dict) -> dict:
    """Search across all Switchboard content: task goals, messages, and message chunks.

    Returns compact result cards (not full task objects).
    Each result: {type, entity_id, title, snippet, relevance_score, author, message_type, created_at}
    """
    query = arguments["query"]
    project_id = arguments.get("project_id")
    limit = min(int(arguments.get("limit", 10)), 30)

    # Embed the query
    service = emb.get_embedding_service()
    query_vector = await service.embed_safe(query)
    if query_vector is None:
        return {
            "error": (
                "Failed to embed query — OPENAI_API_KEY must be set to enable semantic search. "
                "Set the environment variable and restart the server."
            )
        }

    # Run all three searches in parallel
    tasks_coro = db.search_tasks_semantic(
        query_vector=query_vector,
        project_id=project_id,
        limit=limit,
    )
    messages_coro = db.search_messages_semantic(
        query_vector=query_vector,
        project_id=project_id,
        limit=limit,
    )
    chunks_coro = db.search_message_chunks(
        query_vector=query_vector,
        project_id=project_id,
        limit=limit,
    )

    task_hits, message_hits, chunk_hits = await asyncio.gather(
        tasks_coro, messages_coro, chunks_coro
    )

    # Build set of message_ids covered by chunks — deduplicate messages
    chunk_message_ids = {hit["message_id"] for hit in chunk_hits}

    candidates = []

    # --- Task hits ---
    for hit in task_hits:
        task_id = hit.get("task_id")
        if not task_id:
            continue
        goal = hit.get("goal") or ""
        candidates.append({
            "type": "task",
            "entity_id": task_id,
            "title": goal,
            "snippet": _make_search_snippet(goal),
            "relevance_score": round(hit["similarity"], 4),
            "author": None,
            "message_type": None,
            "created_at": hit.get("created_at"),
        })

    # --- Message hits (skip if chunk exists for same message) ---
    for hit in message_hits:
        if hit["message_id"] in chunk_message_ids:
            continue
        score = round(compute_relevance_score(
            hit["similarity"], hit.get("type"), hit.get("pinned", False)
        ), 4)
        msg_type = "task_message" if hit.get("task_id") else "conversation_message"
        candidates.append({
            "type": msg_type,
            "entity_id": str(hit["message_id"]),
            "title": hit.get("title"),
            "snippet": _make_search_snippet(hit.get("content") or ""),
            "relevance_score": score,
            "author": hit.get("author"),
            "message_type": hit.get("type"),
            "created_at": hit.get("created_at"),
        })

    # --- Chunk hits ---
    for hit in chunk_hits:
        score = round(compute_relevance_score(
            hit["similarity"], hit.get("type"), hit.get("pinned", False)
        ), 4)
        msg_type = "task_message" if hit.get("task_id") else "conversation_message"
        title = hit.get("title") or hit.get("chunk_heading")
        candidates.append({
            "type": "chunk",
            "entity_id": str(hit["message_id"]),
            "title": title,
            "snippet": _make_search_snippet(hit.get("chunk_content") or ""),
            "relevance_score": score,
            "author": hit.get("author"),
            "message_type": hit.get("type"),
            "created_at": hit.get("created_at"),
        })

    # Sort by relevance descending, take top limit
    candidates.sort(key=lambda r: r["relevance_score"], reverse=True)
    results = candidates[:limit]

    return {"results": results, "total_candidates": len(candidates)}
