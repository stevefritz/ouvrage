"""Unified search handler — searches tasks, messages, and chunks in one call."""

import asyncio

import switchboard.db as db
from switchboard.embeddings import service as emb
from switchboard.embeddings.service import compute_relevance_score


async def _handle_search(arguments: dict) -> dict:
    """Search across all Switchboard content: task goals, messages, and message chunks.

    Groups all matches by task and returns task objects in relevance order.
    A task appears once even if it matched on goal, spec, and messages.
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

    # Build set of message_ids covered by chunks — these will be deduplicated out
    # (chunk is more specific than the parent message)
    chunk_message_ids = {hit["message_id"] for hit in chunk_hits}

    # Group by task_id, keeping the best relevance score per task
    best_score: dict[str, float] = {}

    # --- Task hits ---
    for hit in task_hits:
        task_id = hit.get("task_id")
        if not task_id:
            continue
        score = round(hit["similarity"], 4)
        if score > best_score.get(task_id, -1):
            best_score[task_id] = score

    # --- Message hits (skip if a chunk from the same message is present) ---
    for hit in message_hits:
        if hit["message_id"] in chunk_message_ids:
            continue
        task_id = hit.get("task_id")
        if not task_id:
            continue
        score = round(compute_relevance_score(
            hit["similarity"], hit.get("type"), hit.get("pinned", False)
        ), 4)
        if score > best_score.get(task_id, -1):
            best_score[task_id] = score

    # --- Chunk hits ---
    for hit in chunk_hits:
        task_id = hit.get("task_id")
        if not task_id:
            continue
        score = round(compute_relevance_score(
            hit["similarity"], hit.get("type"), hit.get("pinned", False)
        ), 4)
        if score > best_score.get(task_id, -1):
            best_score[task_id] = score

    # Sort task_ids by best score descending, take top limit
    ranked_task_ids = sorted(best_score, key=lambda tid: best_score[tid], reverse=True)[:limit]

    # Fetch full task objects in parallel
    task_objects = await asyncio.gather(
        *(db.get_task(tid) for tid in ranked_task_ids)
    )

    # Filter out any tasks that no longer exist (deleted between search and fetch)
    results = [t for t in task_objects if t is not None]

    return {"results": results, "total_candidates": len(best_score)}
