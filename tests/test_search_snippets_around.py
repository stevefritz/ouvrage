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

class TestStripMarkdown:
    def test_strips_headers(self):
        text = "# My Header\n\nSome content here."
        result = _strip_markdown(text)
        assert "#" not in result
        assert "My Header" in result
        assert "Some content here." in result

    def test_strips_fenced_code_blocks(self):
        text = "Before\n```python\ncode here\n```\nAfter"
        result = _strip_markdown(text)
        assert "```" not in result
        assert "code here" not in result
        assert "Before" in result
        assert "After" in result

    def test_strips_inline_code(self):
        text = "Use `some_func()` to call it."
        result = _strip_markdown(text)
        assert "`" not in result
        assert "some_func()" not in result
        assert "Use" in result
        assert "to call it." in result

    def test_strips_bold(self):
        text = "This is **bold text** here."
        result = _strip_markdown(text)
        assert "**" not in result
        assert "bold text" in result

    def test_strips_italic(self):
        text = "This is *italic text* here."
        result = _strip_markdown(text)
        assert "*" not in result
        assert "italic text" in result

    def test_strips_links(self):
        text = "See [the docs](https://example.com) for details."
        result = _strip_markdown(text)
        assert "[" not in result
        assert "https://example.com" not in result
        assert "the docs" in result

    def test_collapses_whitespace(self):
        text = "Line one\n\nLine two\n\nLine three"
        result = _strip_markdown(text)
        assert "\n" not in result
        assert "Line one" in result
        assert "Line two" in result

    def test_empty_string(self):
        assert _strip_markdown("") == ""

    def test_plain_text_unchanged(self):
        text = "This is plain text with no markdown."
        result = _strip_markdown(text)
        assert result == text


class TestMakeSearchSnippet:
    def test_truncates_at_200_chars(self):
        long_text = "A" * 300
        result = _make_search_snippet(long_text)
        assert len(result) <= 201  # 200 chars + ellipsis
        assert result.endswith("…")

    def test_no_truncation_under_200(self):
        short_text = "Short content"
        result = _make_search_snippet(short_text)
        assert result == short_text
        assert not result.endswith("…")

    def test_strips_markdown_before_truncating(self):
        # A message that's short after stripping but long with markdown
        text = "# " + "A" * 10 + "\n\n```python\n" + "x" * 100 + "\n```\nActual content."
        result = _make_search_snippet(text)
        assert "```" not in result
        assert "#" not in result

    def test_none_input_returns_empty(self):
        result = _make_search_snippet(None)
        assert result == ""


# ---------------------------------------------------------------------------
# Search result shape
# ---------------------------------------------------------------------------

class TestSearchResultShape:
    def _encode(self, v):
        return struct.pack(f"{len(v)}f", *v)

    def _vec(self, dim=4, idx=0):
        v = [0.0] * dim
        v[idx % dim] = 1.0
        return v

    async def test_task_result_shape(self, db, sample_project):
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector
        from switchboard.server.handlers.search import _handle_search

        vec = self._vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        task = await db.create_task(
            id="test-project/shape-task",
            project_id="test-project",
            goal="Test shape of task result",
        )
        await db.set_task_embedding(task["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "shape"})
            matching = [r for r in result["results"] if r["entity_id"] == task["id"]]
            assert len(matching) == 1
            r = matching[0]
            # Required fields
            assert r["type"] == "task"
            assert r["entity_id"] == task["id"]  # task_id string
            assert isinstance(r["snippet"], str)
            assert isinstance(r["relevance_score"], float)
            assert r["author"] is None
            assert r["message_type"] is None
            assert "created_at" in r
            assert "title" in r
        finally:
            set_embedding_service(None)

    async def test_message_result_shape(self, db, sample_project):
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector
        from switchboard.server.handlers.search import _handle_search

        vec = self._vec(4, 1)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        task = await db.create_task(
            id="test-project/msg-shape-task",
            project_id="test-project",
            goal="Task for message shape test",
        )
        msg = await db.post_task_message(
            task_id=task["id"],
            author="human",
            content="Message content for shape testing with enough text.",
            type="note",
        )
        await db.set_message_embedding(msg["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "message content shape"})
            matching = [r for r in result["results"] if r.get("entity_id") == str(msg["id"])]
            assert len(matching) == 1
            r = matching[0]
            assert r["type"] in ("task_message", "conversation_message", "chunk")
            assert r["entity_id"] == str(msg["id"])  # message_id as string
            assert isinstance(r["snippet"], str)
            assert isinstance(r["relevance_score"], float)
            assert r["author"] == "human"
            assert r["message_type"] == "note"
            assert "created_at" in r
        finally:
            set_embedding_service(None)

    async def test_entity_id_is_string_for_messages(self, db, sample_project):
        """entity_id must always be a string (message_id as str for messages/chunks)."""
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector
        from switchboard.server.handlers.search import _handle_search

        vec = self._vec(4, 2)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        task = await db.create_task(
            id="test-project/entity-id-task",
            project_id="test-project",
            goal="Entity ID type check",
        )
        msg = await db.post_task_message(
            task_id=task["id"],
            author="human",
            content="Content for entity ID type check with enough text.",
            type="progress",
        )
        await db.set_message_embedding(msg["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "entity ID"})
            for r in result["results"]:
                assert isinstance(r["entity_id"], str)
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Total response size
# ---------------------------------------------------------------------------

class TestSearchResponseSize:
    async def test_10_results_under_10k_chars(self, db, sample_project):
        """10 results should serialize to well under 10K characters."""
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector
        from switchboard.server.handlers.search import _handle_search

        vec = [1.0, 0.0, 0.0, 0.0]

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        # Create 10 tasks with long goals
        for i in range(10):
            t = await db.create_task(
                id=f"test-project/size-task-{i}",
                project_id="test-project",
                goal=f"Task {i}: " + "This is a detailed goal description. " * 10,
            )
            from switchboard.embeddings.service import encode_vector
            await db.set_task_embedding(t["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "task description", "limit": 10})
            serialized = json.dumps(result)
            assert len(serialized) < 10_000, f"Response too large: {len(serialized)} chars"
        finally:
            set_embedding_service(None)


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

    async def test_around_does_not_require_conversation_id(self, db, sample_conversation):
        from switchboard.server.handlers.conversations import _handle_read

        result = await db.read_messages("widget-redesign", last_n=10)
        msgs = result["messages"]
        target = msgs[0]

        # No conversation_id provided — should still work
        resp = await _handle_read({"around": target["id"]})
        assert "error" not in resp
        assert "messages" in resp

    async def test_around_resolves_conversation_id(self, db, sample_conversation):
        from switchboard.server.handlers.conversations import _handle_read

        result = await db.read_messages("widget-redesign", last_n=10)
        target = result["messages"][0]

        resp = await _handle_read({"around": target["id"]})
        assert resp.get("conversation_id") == "widget-redesign"

    async def test_around_returns_chronological_order(self, db, sample_conversation):
        from switchboard.server.handlers.conversations import _handle_read

        result = await db.read_messages("widget-redesign", last_n=10)
        msgs = result["messages"]
        if len(msgs) < 3:
            pytest.skip("Need at least 3 messages")
        target = msgs[1]  # Middle message

        resp = await _handle_read({"around": target["id"]})
        returned_ids = [m["id"] for m in resp["messages"]]
        # Messages should be in ascending ID order (chronological)
        assert returned_ids == sorted(returned_ids)

    async def test_around_not_found_returns_error(self, db, sample_conversation):
        from switchboard.server.handlers.conversations import _handle_read

        resp = await _handle_read({"around": 99999999})
        assert "error" in resp

    async def test_around_window_is_3(self, db):
        """Window defaults to 3: 1 before + target + 1 after."""
        from switchboard.server.handlers.conversations import _handle_read

        await db.create_conversation(id="around-test-conv", project="test-project", goal="Around test")
        # Create 5 messages
        msgs = []
        for i in range(5):
            m = await db.post_message(
                conversation_id="around-test-conv",
                author="human",
                content=f"Message {i}",
            )
            msgs.append(m)

        # Target the 3rd message (index 2), should get msgs[1], msgs[2], msgs[3]
        target = msgs[2]
        resp = await _handle_read({"around": target["id"]})
        assert "messages" in resp
        assert len(resp["messages"]) == 3
        ids = [m["id"] for m in resp["messages"]]
        assert msgs[1]["id"] in ids
        assert msgs[2]["id"] in ids
        assert msgs[3]["id"] in ids

    async def test_around_at_edge_returns_fewer_messages(self, db):
        """When target is first message, only target + 1 after returned."""
        from switchboard.server.handlers.conversations import _handle_read

        await db.create_conversation(id="around-edge-conv", project="test-project", goal="Edge test")
        msgs = []
        for i in range(3):
            m = await db.post_message(
                conversation_id="around-edge-conv",
                author="human",
                content=f"Edge message {i}",
            )
            msgs.append(m)

        # Target the first message — no message before it
        resp = await _handle_read({"around": msgs[0]["id"]})
        assert "messages" in resp
        assert len(resp["messages"]) <= 3  # Can't exceed window
        ids = [m["id"] for m in resp["messages"]]
        assert msgs[0]["id"] in ids


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

    async def test_around_does_not_require_task_id(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_read_task_messages

        msg = await db.post_task_message(
            task_id=sample_task["id"],
            author="cc-worker",
            content="Some result content here for testing around.",
            type="result",
        )

        # No task_id provided — should still work via around
        resp = await _handle_read_task_messages({"around": msg["id"]})
        assert "error" not in resp
        assert "messages" in resp

    async def test_around_resolves_task_id(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_read_task_messages

        msg = await db.post_task_message(
            task_id=sample_task["id"],
            author="cc-worker",
            content="Some content for task_id resolution test.",
            type="progress",
        )

        resp = await _handle_read_task_messages({"around": msg["id"]})
        assert resp.get("task_id") == sample_task["id"]

    async def test_around_not_found_returns_error(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_read_task_messages

        resp = await _handle_read_task_messages({"around": 99999999})
        assert "error" in resp

    async def test_no_task_id_no_around_returns_error(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_read_task_messages

        resp = await _handle_read_task_messages({})
        assert "error" in resp
        assert "task_id" in resp["error"]


# ---------------------------------------------------------------------------
# window parameter — caller controls context size
# ---------------------------------------------------------------------------

class TestWindowParameter:
    async def test_window_5_returns_5_messages(self, db):
        """Caller can pass window=5 to get 2 before + target + 2 after."""
        from switchboard.server.handlers.conversations import _handle_read

        await db.create_conversation(id="window-test-conv", project="test-project", goal="Window test")
        msgs = []
        for i in range(7):
            m = await db.post_message(
                conversation_id="window-test-conv",
                author="human",
                content=f"Window message {i}",
            )
            msgs.append(m)

        # Target the middle message (index 3), with window=5 should get msgs[1..5]
        target = msgs[3]
        resp = await _handle_read({"around": target["id"], "window": 5})
        assert "messages" in resp
        assert len(resp["messages"]) == 5
        ids = [m["id"] for m in resp["messages"]]
        assert msgs[2]["id"] in ids  # 2 before
        assert msgs[3]["id"] in ids  # target
        assert msgs[4]["id"] in ids  # 2 after

    async def test_window_default_is_3(self, db):
        """Without window param, defaults to 3."""
        from switchboard.server.handlers.conversations import _handle_read

        await db.create_conversation(id="window-default-conv", project="test-project", goal="Window default test")
        msgs = []
        for i in range(5):
            m = await db.post_message(
                conversation_id="window-default-conv",
                author="human",
                content=f"Default window message {i}",
            )
            msgs.append(m)

        target = msgs[2]
        resp = await _handle_read({"around": target["id"]})
        assert len(resp["messages"]) == 3

    async def test_window_passed_through_read_task_messages(self, db, sample_task):
        """window param works for read_task_messages too."""
        from switchboard.server.handlers.tasks import _handle_read_task_messages

        msgs = []
        for i in range(7):
            m = await db.post_task_message(
                task_id=sample_task["id"],
                author="cc-worker",
                content=f"Window task message {i}",
                type="progress",
            )
            msgs.append(m)

        target = msgs[3]
        resp = await _handle_read_task_messages({"around": target["id"], "window": 5})
        assert "messages" in resp
        assert len(resp["messages"]) == 5
