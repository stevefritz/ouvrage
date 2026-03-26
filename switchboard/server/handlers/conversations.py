"""Conversation tool handlers."""

import asyncio
import logging

import switchboard.db as db
from switchboard.embeddings import service as emb
from switchboard.server.handlers.common import _embed_message_async

log = logging.getLogger("switchboard.server")


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
    )
    if arguments.get("content"):
        msg = await db.post_message(
            conversation_id=arguments["id"],
            author=arguments.get("author", "human"),
            content=arguments["content"],
            type=arguments.get("type"),
            title=arguments.get("title"),
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
                    author="switchboard",
                    type="note",
                    content=nudge,
                )
                log.info(f"Injected conversation update for '{conversation_id}' into task {task_id}")
        except Exception as e:
            log.warning(f"Reactive injection failed for conversation '{conversation_id}': {e}")

    return result


async def _handle_read(arguments):
    return await db.read_messages(
        conversation_id=arguments["conversation_id"],
        after=arguments.get("after"),
        last_n=arguments.get("last_n"),
        since=arguments.get("since"),
        author=arguments.get("author"),
        type=arguments.get("type"),
    )


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

    # Retrieve candidates with raw similarity scores
    candidates = await db.search_messages_semantic(
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

    # Format output: truncate content to 500 chars
    results = []
    for r in top:
        results.append({
            "message_id": r["message_id"],
            "conversation_id": r["conversation_id"],
            "task_id": r["task_id"],
            "author": r["author"],
            "type": r["type"],
            "title": r["title"],
            "content": (r["content"] or "")[:500],
            "relevance_score": r["relevance_score"],
            "created_at": r["created_at"],
        })

    return {"results": results, "total_candidates": len(candidates)}
