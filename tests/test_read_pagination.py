"""Tests for read pagination, message-level access, and summary mode.

Covers: message_id lookup, offset/limit pagination, summary mode,
pinned_only filter, attempt filter, total/has_more metadata,
and backward compat with last_n.
"""

import pytest


# ===========================================================================
# Conversation read() — message_id, pagination, summary
# ===========================================================================

class TestReadMessageById:
    """read() with message_id returns a single message."""

    async def test_message_id_returns_single_message(self, db, sample_conversation):
        from switchboard.server.handlers.conversations import _handle_read
        # Get messages to find an ID
        result = await db.read_messages("widget-redesign", last_n=10)
        msg = result["messages"][0]

        resp = await _handle_read({
            "conversation_id": "widget-redesign",
            "message_id": msg["id"],
        })
        assert "message" in resp
        assert resp["message"]["id"] == msg["id"]
        assert "content" in resp["message"]

    async def test_message_id_wrong_conversation_returns_error(self, db, sample_conversation):
        from switchboard.server.handlers.conversations import _handle_read
        # Create a second conversation
        await db.create_conversation(id="other-convo", project="test-project", goal="Other")
        await db.post_message(conversation_id="other-convo", author="test", content="hello")

        result = await db.read_messages("other-convo", last_n=1)
        other_msg_id = result["messages"][0]["id"]

        resp = await _handle_read({
            "conversation_id": "widget-redesign",
            "message_id": other_msg_id,
        })
        assert "error" in resp
        assert "does not belong" in resp["error"]

    async def test_message_id_not_found(self, db, sample_conversation):
        from switchboard.server.handlers.conversations import _handle_read
        resp = await _handle_read({
            "conversation_id": "widget-redesign",
            "message_id": 99999,
        })
        assert "error" in resp
        assert "not found" in resp["error"]


class TestReadSummary:
    """read() with summary=true returns lightweight objects."""


    async def test_summary_preview_truncates_at_150(self, db, sample_project):
        from switchboard.server.handlers.conversations import _handle_read

        await db.create_conversation(id="long-convo", project="test-project", goal="Long")
        long_content = "x" * 300
        await db.post_message(conversation_id="long-convo", author="test", content=long_content)

        resp = await _handle_read({
            "conversation_id": "long-convo",
            "summary": True,
        })
        msg = resp["messages"][0]
        assert msg["char_count"] == 300
        assert len(msg["preview"]) == 153  # 150 + "..."
        assert msg["preview"].endswith("...")


class TestReadPinnedOnly:
    """read() with pinned_only=true returns only pinned messages."""


    async def test_pinned_only_with_summary(self, db, sample_conversation):
        from switchboard.server.handlers.conversations import _handle_read

        resp = await _handle_read({
            "conversation_id": "widget-redesign",
            "pinned_only": True,
            "summary": True,
        })
        assert len(resp["messages"]) == 1
        assert "preview" in resp["messages"][0]
        assert "content" not in resp["messages"][0]


# ===========================================================================
# Task read_task_messages() — message_id, pagination, summary, attempt
# ===========================================================================

class TestReadTaskMessageById:
    """read_task_messages() with message_id."""

    async def test_message_id_returns_single(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_read_task_messages

        task_id = sample_task["id"]
        await db.post_task_message(task_id=task_id, author="cc-worker", content="Progress update")

        result = await db.read_task_messages(task_id, last_n=1)
        msg_id = result["messages"][-1]["id"]

        resp = await _handle_read_task_messages({
            "task_id": task_id,
            "message_id": msg_id,
        })
        assert "message" in resp
        assert resp["message"]["id"] == msg_id

    async def test_message_id_wrong_task_returns_error(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_read_task_messages

        # Create another task
        task2 = await db.create_task(
            id="test-project/other-task",
            project_id="test-project",
            goal="Other task",
            branch="other-branch",
        )
        await db.post_task_message(task_id=task2["id"], author="cc-worker", content="Other msg")
        result = await db.read_task_messages(task2["id"], last_n=1)
        other_msg_id = result["messages"][-1]["id"]

        resp = await _handle_read_task_messages({
            "task_id": sample_task["id"],
            "message_id": other_msg_id,
        })
        assert "error" in resp
        assert "does not belong" in resp["error"]


class TestReadTaskMessagesSummary:
    """read_task_messages() with summary=true."""

    async def test_summary(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_read_task_messages

        task_id = sample_task["id"]
        await db.post_task_message(task_id=task_id, author="cc-worker", content="A" * 200)

        resp = await _handle_read_task_messages({
            "task_id": task_id,
            "summary": True,
        })
        for msg in resp["messages"]:
            assert "preview" in msg
            assert "char_count" in msg
            assert "content" not in msg


class TestReadTaskMessagesAttemptFilter:
    """read_task_messages() with attempt filter."""

    async def _insert_with_attempt(self, db, task_id, content, attempt_number):
        """Insert a message with a specific attempt_number directly."""
        from switchboard.db.connection import get_db
        from switchboard.db._helpers import now_iso
        async with get_db() as conn:
            ts = now_iso()
            await conn.execute(
                """INSERT INTO messages (task_id, author, type, content, pinned, created_at, attempt_number)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (task_id, "cc-worker", "progress", content, False, ts, attempt_number),
            )
            await conn.commit()


    async def test_attempt_filter_with_summary(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_read_task_messages

        task_id = sample_task["id"]
        await self._insert_with_attempt(db, task_id, "Attempt 1", 1)
        await self._insert_with_attempt(db, task_id, "Attempt 2", 2)

        resp = await _handle_read_task_messages({
            "task_id": task_id,
            "attempt": 1,
            "summary": True,
        })
        assert resp["total"] == 1
        assert "preview" in resp["messages"][0]
        assert "content" not in resp["messages"][0]
