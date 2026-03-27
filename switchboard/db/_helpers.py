"""Shared helpers used across multiple db submodules."""
from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _strip_embedding(msg: dict) -> dict:
    """Remove the embedding field from a message dict before returning to callers.

    Embeddings are internal-only (semantic search). They're never useful to
    external callers and can be massive binary blobs. Strip defensively so that
    adding an embedding column later never leaks into API responses.
    """
    msg.pop("embedding", None)
    return msg


async def _read_messages(
    filter_column: str, filter_value: str,
    last_n: int | None = None, since: str | None = None,
    after: int | None = None, author: str | None = None,
    type: str | None = None,
    offset: int | None = None, limit: int | None = None,
    pinned_only: bool = False, attempt: int | None = None,
) -> dict:
    """Shared implementation for read_messages() and read_task_messages().

    Two modes:
    - **last_n mode** (backward compat): when last_n is set, ignores offset/limit,
      returns pinned messages at top followed by last N non-pinned messages.
    - **paginated mode** (default): uses offset/limit with natural created_at ASC
      ordering. Returns total count and has_more for pagination.
    """
    from switchboard.db.connection import get_db

    # last_n takes precedence — backward compat path
    if last_n is not None:
        return await _read_messages_last_n(
            filter_column, filter_value, last_n=last_n, since=since,
            after=after, author=author, type=type, attempt=attempt,
        )

    # Paginated path
    effective_limit = min(limit or 50, 50)
    effective_offset = offset or 0

    async with get_db() as db:
        conditions = [f"{filter_column} = ?"]
        params: list = [filter_value]

        if after is not None:
            conditions.append("id > ?")
            params.append(after)
        if since:
            conditions.append("created_at > ?")
            params.append(since)
        if author:
            conditions.append("author = ?")
            params.append(author)
        if type:
            conditions.append("type = ?")
            params.append(type)
        if pinned_only:
            conditions.append("pinned = TRUE")
        if attempt is not None:
            conditions.append("attempt_number = ?")
            params.append(attempt)

        where = " AND ".join(conditions)

        # Total count for pagination metadata
        count_row = await db.execute_fetchall(
            f"SELECT COUNT(*) as cnt FROM messages WHERE {where}", params,
        )
        total = count_row[0]["cnt"] if count_row else 0

        # Fetch page
        query = (
            f"SELECT * FROM messages WHERE {where} "
            f"ORDER BY created_at ASC LIMIT ? OFFSET ?"
        )
        rows = await db.execute_fetchall(query, params + [effective_limit, effective_offset])
        messages = [_strip_embedding(dict(r)) for r in rows]

        cursor = max((m["id"] for m in messages), default=after or 0)
        has_more = (effective_offset + len(messages)) < total

        return {
            "messages": messages,
            "cursor": cursor,
            "total": total,
            "has_more": has_more,
        }


async def _read_messages_last_n(
    filter_column: str, filter_value: str,
    last_n: int, since: str | None = None,
    after: int | None = None, author: str | None = None,
    type: str | None = None, attempt: int | None = None,
) -> dict:
    """Backward-compat path: pinned at top + last N non-pinned messages."""
    from switchboard.db.connection import get_db

    async with get_db() as db:
        # Get pinned messages first
        pinned_conds = [f"{filter_column} = ?", "pinned = TRUE"]
        pinned_params: list = [filter_value]
        if attempt is not None:
            pinned_conds.append("attempt_number = ?")
            pinned_params.append(attempt)
        pinned_rows = await db.execute_fetchall(
            f"SELECT * FROM messages WHERE {' AND '.join(pinned_conds)}",
            pinned_params,
        )
        pinned = [_strip_embedding(dict(r)) for r in pinned_rows]
        pinned_ids = {m["id"] for m in pinned}

        # Build query for non-pinned messages
        conditions = [f"{filter_column} = ?", "pinned = FALSE"]
        params: list = [filter_value]

        if after is not None:
            conditions.append("id > ?")
            params.append(after)
        if since:
            conditions.append("created_at > ?")
            params.append(since)
        if author:
            conditions.append("author = ?")
            params.append(author)
        if type:
            conditions.append("type = ?")
            params.append(type)
        if attempt is not None:
            conditions.append("attempt_number = ?")
            params.append(attempt)

        where = " AND ".join(conditions)
        query = (
            f"SELECT * FROM (SELECT * FROM messages WHERE {where} "
            f"ORDER BY created_at DESC LIMIT ?) ORDER BY created_at ASC"
        )
        params.append(last_n)

        rows = await db.execute_fetchall(query, params)
        messages = [_strip_embedding(dict(r)) for r in rows if r["id"] not in pinned_ids]

        # Mark pinned messages
        for m in pinned:
            m["_pinned_marker"] = True

        all_messages = pinned + messages
        cursor = max((m["id"] for m in all_messages), default=after or 0)

        return {"messages": all_messages, "cursor": cursor}


async def _list_with_aggregates(
    where_clause: str, params: list,
) -> list[dict]:
    """Shared implementation for board() and list_conversations().

    Uses a CTE with ROW_NUMBER() to get last message info in one pass
    instead of three correlated subqueries.
    """
    from switchboard.db.connection import get_db

    async with get_db() as db:
        query = f"""
            WITH latest_msg AS (
                SELECT conversation_id, author, title, created_at,
                       ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY created_at DESC) as rn
                FROM messages
                WHERE conversation_id IS NOT NULL
            )
            SELECT
                c.id, c.project, c.goal, c.archived, c.created_at, c.updated_at,
                COUNT(m.id) as message_count,
                lm.author as last_message_author,
                lm.title as last_message_title,
                lm.created_at as last_message_at,
                EXISTS(SELECT 1 FROM messages WHERE conversation_id = c.id AND pinned = TRUE) as has_pinned
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.id
            LEFT JOIN latest_msg lm ON lm.conversation_id = c.id AND lm.rn = 1
            {where_clause}
            GROUP BY c.id
            ORDER BY c.updated_at DESC
        """
        rows = await db.execute_fetchall(query, params)
        return [dict(r) for r in rows]


def _make_snippet(content: str, query: str) -> str:
    """Extract a ~120-char snippet around the first match of query in content."""
    lower_content = content.lower()
    idx = lower_content.find(query.lower())
    if idx >= 0:
        start = max(0, idx - 50)
        end = min(len(content), idx + len(query) + 50)
        return ("..." if start > 0 else "") + content[start:end] + ("..." if end < len(content) else "")
    return content[:120] + ("..." if len(content) > 120 else "")


def _determine_attempt_outcome(messages: list[dict], is_last: bool, has_next: bool) -> str:
    """Determine how an attempt ended based on its messages."""
    # Walk messages in reverse to find the most significant terminal event
    for msg in reversed(messages):
        msg_type = msg.get("type") or ""
        title = (msg.get("title") or "").upper()
        author = msg.get("author") or ""

        if author == "dispatcher":
            if msg_type == "test-result":
                if "FAILED" in title or "FAIL" in title:
                    if has_next:
                        return "test-failure"
                    return "test-failure"
                elif "PASSED" in title or "PASS" in title:
                    if not is_last:
                        return "test-failure"  # more attempts followed
            if "WALL CLOCK" in title or "TIMEOUT" in title:
                return "wall-clock-timeout"
            if "TURNS EXHAUSTED" in title or "TURNS" in title:
                return "turns-exhausted"
            if msg_type == "status" and ("ERROR" in title or "FAILED" in title or "DISPATCH ERROR" in title):
                return "error"
            if msg_type == "status" and "COMPLETED" in title:
                return "success"

        if msg_type == "review":
            if "APPROVED" in title:
                if not has_next:
                    return "success"
            elif "CHANGES REQUESTED" in title or "REJECT" in title:
                if has_next:
                    return "review-rejection"
                return "review-rejection"

    if has_next:
        return "retried"
    return "in-progress"
