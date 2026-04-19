"""Conversation tool handlers."""

import asyncio
import logging

import ouvrage.db as db
from ouvrage.embeddings import service as emb
from ouvrage.server.handlers.common import _embed_message_async
from ouvrage.server.context import get_request_user_id, get_request_is_token_auth

log = logging.getLogger("ouvrage.server")

_SYSTEM_AUTHORS = frozenset({"dispatcher", "cc-worker", "switchboard", "ouvrage"})


def _resolve_message_user_id(author: str) -> int | None:
    """Determine user_id to stamp on a message.

    If the request is token-authenticated: always stamp the resolved user_id.
    If fallback (no token): stamp for non-system authors, None for system actors.
    """
    user_id = get_request_user_id()
    if user_id is None:
        return None
    if get_request_is_token_auth():
        return user_id
    # Fallback: only stamp if not a system actor
    return None if author in _SYSTEM_AUTHORS else user_id


async def _handle_board(arguments):
    return await db.board(
        project=arguments.get("project"),
        include_archived=arguments.get("include_archived", False),
    )


async def _handle_create_conversation(arguments):
    result = await db.create_conversation(
        id=arguments["id"],
        project=arguments["project"],
        goal=arguments["goal"],
        claude_chat_url=arguments.get("claude_chat_url"),
        created_by=get_request_user_id(),
    )
    if arguments.get("content"):
        initial_author = arguments.get("author", "human")
        msg = await db.post_message(
            conversation_id=arguments["id"],
            author=initial_author,
            content=arguments["content"],
            type=arguments.get("type"),
            title=arguments.get("title"),
            user_id=_resolve_message_user_id(initial_author),
        )
        result["initial_message"] = msg
        asyncio.create_task(
            _embed_message_async(msg["id"], arguments["content"], arguments.get("type"))
        )
    return result


async def _handle_post(arguments):
    conversation_id = arguments["conversation_id"]
    author = arguments["author"]
    msg_type = arguments.get("type")

    result = await db.post_message(
        conversation_id=conversation_id,
        author=author,
        content=arguments["content"],
        type=msg_type,
        title=arguments.get("title"),
        pinned=arguments.get("pinned", False),
        user_id=_resolve_message_user_id(author),
    )

    # Async embed — fire and forget, doesn't block the response
    asyncio.create_task(
        _embed_message_async(result["id"], arguments["content"], msg_type)
    )

    # Reactive injection: nudge any working tasks linked to this conversation.
    # Guard: skip if author is cc-worker (prevent feedback loops) or type is status (reduce noise).
    if author != "cc-worker" and msg_type != "status":
        try:
            task_ids = await db.get_working_tasks_for_conversation(conversation_id)
            for task_id in task_ids:
                title = arguments.get("title") or ""
                content = arguments.get("content", "")
                preview = title if title else content[:200]
                nudge = (
                    f"📌 Linked conversation '{conversation_id}' was just updated.\n"
                    f"New message from {author}: \"{preview}\"\n"
                    f"This may be relevant to your current work. "
                    f"Use read(conversation_id='{conversation_id}', last_n=1) to see the full message if needed."
                )
                await db.post_task_message(
                    task_id=task_id,
                    author="ouvrage",
                    type="note",
                    content=nudge,
                )
                log.info(f"Injected conversation update for '{conversation_id}' into task {task_id}")
        except Exception as e:
            log.warning(f"Reactive injection failed for conversation '{conversation_id}': {e}")

    return result


def _summarize_messages(result: dict) -> dict:
    """Transform messages to summary mode: replace content with preview + char_count."""
    summarized = []
    for m in result["messages"]:
        content = m.get("content") or ""
        preview = content[:150]
        if len(content) > 150:
            preview += "..."
        summarized.append({
            "id": m["id"],
            "title": m.get("title"),
            "type": m.get("type"),
            "author": m.get("author"),
            "created_at": m.get("created_at"),
            "pinned": m.get("pinned", False),
            "char_count": len(content),
            "preview": preview,
        })
    result["messages"] = summarized
    return result


async def _handle_read(arguments):
    # Around mode — center on a specific message, resolve conversation internally
    around = arguments.get("around")
    if around is not None:
        return await db.read_messages_around(message_id=around, window=arguments.get("window", 3))

    conversation_id = arguments.get("conversation_id")
    if not conversation_id:
        return {"error": "conversation_id is required when around is not set"}

    # Single message lookup — ignores all other params
    message_id = arguments.get("message_id")
    if message_id is not None:
        msg = await db.get_message_by_id(message_id)
        if msg is None:
            return {"error": f"Message {message_id} not found"}
        if msg.get("conversation_id") != conversation_id:
            return {"error": f"Message {message_id} does not belong to conversation '{conversation_id}'"}
        return {"message": msg}

    result = await db.read_messages(
        conversation_id=conversation_id,
        after=arguments.get("after"),
        last_n=arguments.get("last_n"),
        since=arguments.get("since"),
        author=arguments.get("author"),
        type=arguments.get("type"),
        offset=arguments.get("offset"),
        limit=arguments.get("limit"),
        pinned_only=arguments.get("pinned_only", False),
    )

    if arguments.get("summary"):
        result = _summarize_messages(result)

    return result


async def _handle_get_pinned(arguments):
    result = await db.get_pinned(arguments["conversation_id"])
    return result if result else {"message": "No pinned message in this conversation"}


async def _handle_pin(arguments):
    return await db.pin_message(arguments["message_id"])


async def _handle_conversations(arguments):
    return await db.list_conversations(
        project=arguments.get("project"),
        search=arguments.get("search"),
    )


async def _handle_archive(arguments):
    return await db.archive_conversation(arguments["conversation_id"])


async def _handle_search_conversations(arguments):
    query = arguments["query"]
    max_results = min(int(arguments.get("max_results", 5)), 20)
    conversation_id = arguments.get("conversation_id")
    project_id = arguments.get("project_id")
    type_filter = arguments.get("type_filter")

    # Embed the query
    service = emb.get_embedding_service()
    query_vector = await service.embed_safe(query)
    if query_vector is None:
        return {"error": "Failed to embed query — check OPENAI_API_KEY and service availability"}

    # Retrieve message-level candidates with raw similarity scores
    candidates = await db.search_messages_semantic(
        query_vector=query_vector,
        conversation_id=conversation_id,
        project_id=project_id,
        type_filter=type_filter,
        limit=max_results,
    )

    # Also search message chunks for paragraph-level hits
    chunk_hits = await db.search_message_chunks(
        query_vector=query_vector,
        conversation_id=conversation_id,
        project_id=project_id,
        type_filter=type_filter,
        limit=max_results,
    )

    # Apply type weights + pinned boost, then re-rank
    for c in candidates:
        c["relevance_score"] = round(
            emb.compute_relevance_score(c["similarity"], c["type"], c["pinned"]),
            4,
        )

    candidates.sort(key=lambda r: r["relevance_score"], reverse=True)
    top = candidates[:max_results]

    # Build set of message_ids already in top results for dedup
    seen_message_ids = {r["message_id"] for r in top}

    # Group chunk hits by message_id, keep best similarity per message
    chunk_groups: dict[int, list[dict]] = {}
    for ch in chunk_hits:
        mid = ch["message_id"]
        chunk_groups.setdefault(mid, []).append(ch)

    # Format output: truncate content to 500 chars
    results = []
    for r in top:
        entry = {
            "message_id": r["message_id"],
            "conversation_id": r["conversation_id"],
            "task_id": r["task_id"],
            "author": r["author"],
            "type": r["type"],
            "title": r["title"],
            "content": (r["content"] or "")[:500],
            "relevance_score": r["relevance_score"],
            "created_at": r["created_at"],
        }
        # If this message also had chunk hits, attach them
        if r["message_id"] in chunk_groups:
            msg_chunks = chunk_groups.pop(r["message_id"])
            best = max(msg_chunks, key=lambda c: c["similarity"])
            entry["chunk_heading"] = best["chunk_heading"]
            entry["context_chunks"] = [
                {"chunk_index": best["chunk_index"], "heading": best["chunk_heading"],
                 "content": (best["chunk_content"] or "")[:500]}
            ] + [
                {"chunk_index": ac["chunk_index"], "heading": ac["heading"],
                 "content": (ac["content"] or "")[:500]}
                for ac in best.get("context_chunks", [])
            ]
        results.append(entry)

    # Add chunk-only hits (messages not already in results)
    for mid, chunks in chunk_groups.items():
        if mid in seen_message_ids:
            continue
        if len(results) >= max_results:
            break
        best = max(chunks, key=lambda c: c["similarity"])
        relevance = round(
            emb.compute_relevance_score(best["similarity"], best["type"], best["pinned"]),
            4,
        )
        results.append({
            "message_id": mid,
            "conversation_id": best["conversation_id"],
            "task_id": best["task_id"],
            "author": best["author"],
            "type": best["type"],
            "title": best["title"],
            "content": (best["chunk_content"] or "")[:500],
            "relevance_score": relevance,
            "created_at": best["created_at"],
            "chunk_heading": best["chunk_heading"],
            "context_chunks": [
                {"chunk_index": best["chunk_index"], "heading": best["chunk_heading"],
                 "content": (best["chunk_content"] or "")[:500]}
            ] + [
                {"chunk_index": ac["chunk_index"], "heading": ac["heading"],
                 "content": (ac["content"] or "")[:500]}
                for ac in best.get("context_chunks", [])
            ],
        })
        seen_message_ids.add(mid)

    # Re-sort unified results by relevance
    results.sort(key=lambda r: r["relevance_score"], reverse=True)

    return {"results": results[:max_results], "total_candidates": len(candidates) + len(chunk_hits)}


async def _handle_search_message_chunks(arguments):
    query = arguments["query"]
    limit = min(int(arguments.get("limit", 5)), 20)
    conversation_id = arguments.get("conversation_id")
    project_id = arguments.get("project_id")

    service = emb.get_embedding_service()
    query_vector = await service.embed_safe(query)
    if query_vector is None:
        return {"error": "Failed to embed query — check OPENAI_API_KEY and service availability"}

    chunk_hits = await db.search_message_chunks(
        query_vector=query_vector,
        conversation_id=conversation_id,
        project_id=project_id,
        limit=limit,
    )

    results = []
    for hit in chunk_hits:
        results.append({
            "message_id": hit["message_id"],
            "conversation_id": hit["conversation_id"],
            "task_id": hit["task_id"],
            "author": hit["author"],
            "type": hit["type"],
            "title": hit["title"],
            "created_at": hit["created_at"],
            "chunk_heading": hit["chunk_heading"],
            "chunk_content": (hit["chunk_content"] or "")[:500],
            "similarity": round(hit["similarity"], 4),
            "context_chunks": [
                {"chunk_index": ac["chunk_index"], "heading": ac["heading"],
                 "content": (ac["content"] or "")[:500]}
                for ac in hit.get("context_chunks", [])
            ],
        })

    return {"results": results}
