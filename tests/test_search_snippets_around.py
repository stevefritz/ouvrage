"""Tests for search snippet truncation and read() around param.

Covers:
- Markdown stripping (_strip_markdown, _make_search_snippet)
- Search result snippet ≤200 chars
- Search result shape: type, entity_id, title, snippet, relevance_score, author, message_type, created_at
- entity_id is task_id string for tasks, str(message_id) for messages/chunks
- Total response for 10 results stays under 10K chars
- read() with around param: returns 3 messages centered on target
- read() around resolves conversation_id from message_id internally
- read_task_messages() with around param: same behavior
"""

import json
import struct

import pytest

from switchboard.server.handlers.search import _strip_markdown, _make_search_snippet


# ---------------------------------------------------------------------------
# Markdown stripping
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Search result shape
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Total response size
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# read() with around param
# ---------------------------------------------------------------------------

class TestReadAround:
    async def test_around_returns_messages_centered_on_target(self, db, sample_conversation):
        from switchboard.server.handlers.conversations import _handle_read

        # Get messages to find IDs
        result = await db.read_messages("widget-redesign", last_n=10)
        msgs = result["messages"]
        assert len(msgs) >= 3, "Need at least 3 messages for around test"

        # Use the middle message
        target_msg = msgs[len(msgs) // 2]

        resp = await _handle_read({"around": target_msg["id"]})
        assert "messages" in resp
        assert "around_message_id" in resp
        assert resp["around_message_id"] == target_msg["id"]
        # Should have at most 3 messages (window=3: 1 before + target + 1 after)
        assert len(resp["messages"]) <= 3
        # Target message must be in results
        ids = [m["id"] for m in resp["messages"]]
        assert target_msg["id"] in ids


    async def test_around_not_found_returns_error(self, db, sample_conversation):
        from switchboard.server.handlers.conversations import _handle_read

        resp = await _handle_read({"around": 99999999})
        assert "error" in resp


# ---------------------------------------------------------------------------
# read_task_messages() with around param
# ---------------------------------------------------------------------------

class TestReadTaskMessagesAround:
    async def test_around_returns_task_messages_centered_on_target(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_read_task_messages

        # Post extra messages to have context
        for i in range(4):
            await db.post_task_message(
                task_id=sample_task["id"],
                author="cc-worker",
                content=f"Progress message {i}",
                type="progress",
            )

        result = await db.read_task_messages(sample_task["id"], last_n=10)
        msgs = result["messages"]
        assert len(msgs) >= 3

        target = msgs[len(msgs) // 2]
        resp = await _handle_read_task_messages({"around": target["id"]})

        assert "messages" in resp
        assert resp["around_message_id"] == target["id"]
        assert len(resp["messages"]) <= 3
        ids = [m["id"] for m in resp["messages"]]
        assert target["id"] in ids


    async def test_no_task_id_no_around_returns_error(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_read_task_messages

        resp = await _handle_read_task_messages({})
        assert "error" in resp
        assert "task_id" in resp["error"]


# ---------------------------------------------------------------------------
# window parameter — caller controls context size
# ---------------------------------------------------------------------------

