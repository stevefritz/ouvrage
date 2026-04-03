"""Unified search handler — searches tasks, messages, and chunks in one call."""

import asyncio

import switchboard.db as db
from switchboard.embeddings import service as emb
from switchboard.embeddings.service import compute_relevance_score


async def _handle_search(arguments: dict) -> dict:
    """Search across all Switchboard content: task goals, messages, and message chunks."""
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

    # Build set of message_ids covered by chunks — these will be deduplicated out
    # (chunk is more specific than the parent message)
    chunk_message_ids = {hit["message_id"] for hit in chunk_hits}

    results = []

    # --- Task results ---
    for hit in task_hits:
        goal = hit.get("goal") or ""
        results.append({
            "type": "task",
            "task_id": hit["task_id"],
            "conversation_id": None,
            "title": goal,
            "snippet": goal[:200],
            "relevance_score": round(hit["similarity"], 4),
            "created_at": None,
        })

    # --- Message results (skip if a chunk from the same message is present) ---
    for hit in message_hits:
        if hit["message_id"] in chunk_message_ids:
            continue
        content = hit.get("content") or ""
        relevance = compute_relevance_score(
            hit["similarity"], hit.get("type"), hit.get("pinned", False)
        )
        if hit.get("task_id"):
            result_type = "task_message"
        else:
            result_type = "conversation_message"
        results.append({
            "type": result_type,
            "task_id": hit.get("task_id"),
            "conversation_id": hit.get("conversation_id"),
            "title": hit.get("title") or "",
            "snippet": content[:200],
            "relevance_score": round(relevance, 4),
            "created_at": hit.get("created_at"),
        })

    # --- Chunk results ---
    for hit in chunk_hits:
        chunk_content = hit.get("chunk_content") or ""
        relevance = compute_relevance_score(
            hit["similarity"], hit.get("type"), hit.get("pinned", False)
        )
        title = hit.get("chunk_heading") or hit.get("title") or ""
        results.append({
            "type": "chunk",
            "task_id": hit.get("task_id"),
            "conversation_id": hit.get("conversation_id"),
            "title": title,
            "snippet": chunk_content[:200],
            "relevance_score": round(relevance, 4),
            "created_at": hit.get("created_at"),
        })

    # Sort by relevance descending, return top limit
    results.sort(key=lambda r: r["relevance_score"], reverse=True)
    return {"results": results[:limit], "total_candidates": len(results)}
