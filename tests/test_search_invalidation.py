"""Tests for search invalidation — DB functions, MCP tool handler, and search integration."""

from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# DB-level tests: upsert_invalidation, delete_invalidation, get_invalidations
# ---------------------------------------------------------------------------

class TestInvalidateDB:
    async def test_upsert_creates_row(self, db):
        """upsert_invalidation stores a new row and returns the record."""
        from switchboard.db.search import upsert_invalidation, get_invalidations

        result = await upsert_invalidation("message", "42", 0.5, reason="stale")

        assert result["entity_type"] == "message"
        assert result["entity_id"] == "42"
        assert result["strength"] == 0.5
        assert result["reason"] == "stale"
        assert result["created_at"]

        rows = await get_invalidations()
        assert len(rows) == 1
        assert rows[0]["entity_type"] == "message"
        assert rows[0]["entity_id"] == "42"

    async def test_upsert_updates_existing_row(self, db):
        """upsert_invalidation updates strength and reason when row exists."""
        from switchboard.db.search import upsert_invalidation, get_invalidations

        await upsert_invalidation("task", "task-1", 0.3, reason="old")
        await upsert_invalidation("task", "task-1", 0.8, reason="new")

        rows = await get_invalidations()
        assert len(rows) == 1
        assert rows[0]["strength"] == 0.8
        assert rows[0]["reason"] == "new"

    async def test_upsert_different_entity_types_are_independent(self, db):
        """Same entity_id with different entity_types creates separate rows."""
        from switchboard.db.search import upsert_invalidation, get_invalidations

        await upsert_invalidation("message", "10", 0.5)
        await upsert_invalidation("task", "10", 0.7)
        await upsert_invalidation("chunk", "10", 0.3)

        rows = await get_invalidations()
        assert len(rows) == 3

    async def test_delete_removes_row_returns_true(self, db):
        """delete_invalidation removes the row and returns True."""
        from switchboard.db.search import upsert_invalidation, delete_invalidation, get_invalidations

        await upsert_invalidation("message", "99", 0.5)
        removed = await delete_invalidation("message", "99")

        assert removed is True
        rows = await get_invalidations()
        assert len(rows) == 0

    async def test_delete_nonexistent_returns_false(self, db):
        """delete_invalidation on a non-existent row returns False."""
        from switchboard.db.search import delete_invalidation

        removed = await delete_invalidation("message", "does-not-exist")
        assert removed is False

    async def test_delete_only_removes_matching_row(self, db):
        """delete_invalidation only removes the specific (entity_type, entity_id) pair."""
        from switchboard.db.search import upsert_invalidation, delete_invalidation, get_invalidations

        await upsert_invalidation("message", "1", 0.5)
        await upsert_invalidation("message", "2", 0.5)
        await delete_invalidation("message", "1")

        rows = await get_invalidations()
        assert len(rows) == 1
        assert rows[0]["entity_id"] == "2"

    async def test_get_invalidations_no_project_filter(self, db):
        """get_invalidations() with no filter returns all rows."""
        from switchboard.db.search import upsert_invalidation, get_invalidations

        await upsert_invalidation("message", "1", 0.5)
        await upsert_invalidation("task", "t-1", 0.3)

        rows = await get_invalidations()
        assert len(rows) == 2

    async def test_upsert_no_reason(self, db):
        """upsert_invalidation without reason stores None."""
        from switchboard.db.search import upsert_invalidation, get_invalidations

        await upsert_invalidation("chunk", "c-5", 0.6)
        rows = await get_invalidations()
        assert rows[0]["reason"] is None


# ---------------------------------------------------------------------------
# Handler-level tests: _handle_invalidate
# ---------------------------------------------------------------------------

class TestInvalidateTool:
    async def test_upsert_strength_positive(self, db):
        """Handler with strength > 0 creates a row and returns the record."""
        from switchboard.server.handlers.invalidations import _handle_invalidate

        result = await _handle_invalidate({
            "entity_type": "message",
            "entity_id": "10",
            "strength": 0.7,
            "reason": "outdated",
        })

        assert result["entity_type"] == "message"
        assert result["entity_id"] == "10"
        assert result["strength"] == 0.7
        assert result["reason"] == "outdated"
        assert "created_at" in result
        assert "removed" not in result

    async def test_upsert_updates_existing(self, db):
        """Calling handler twice updates the strength."""
        from switchboard.server.handlers.invalidations import _handle_invalidate
        from switchboard.db.search import get_invalidations

        await _handle_invalidate({"entity_type": "task", "entity_id": "t-1", "strength": 0.3})
        await _handle_invalidate({"entity_type": "task", "entity_id": "t-1", "strength": 0.9})

        rows = await get_invalidations()
        assert len(rows) == 1
        assert rows[0]["strength"] == 0.9

    async def test_delete_existing_row_returns_removed_true(self, db):
        """strength=0 deletes an existing row and returns removed=true."""
        from switchboard.server.handlers.invalidations import _handle_invalidate
        from switchboard.db.search import upsert_invalidation

        await upsert_invalidation("message", "99", 0.5)
        result = await _handle_invalidate({
            "entity_type": "message",
            "entity_id": "99",
            "strength": 0,
        })

        assert result["entity_type"] == "message"
        assert result["entity_id"] == "99"
        assert result["removed"] is True

    async def test_delete_nonexistent_returns_removed_false(self, db):
        """strength=0 on non-existent entity returns removed=false (no-op)."""
        from switchboard.server.handlers.invalidations import _handle_invalidate

        result = await _handle_invalidate({
            "entity_type": "task",
            "entity_id": "does-not-exist",
            "strength": 0,
        })

        assert result["removed"] is False

    async def test_handler_no_reason(self, db):
        """Handler works without optional reason param."""
        from switchboard.server.handlers.invalidations import _handle_invalidate

        result = await _handle_invalidate({
            "entity_type": "chunk",
            "entity_id": "c-1",
            "strength": 0.4,
        })

        assert result["reason"] is None
        assert result["strength"] == 0.4

    async def test_tool_registered_in_dispatch(self):
        """invalidate tool is registered in TOOL_HANDLERS."""
        from switchboard.server.dispatch import TOOL_HANDLERS

        assert "invalidate" in TOOL_HANDLERS

    async def test_tool_in_tools_list(self):
        """invalidate tool appears in TOOLS (user endpoint)."""
        from switchboard.server.tools import TOOLS

        names = [t.name for t in TOOLS]
        assert "invalidate" in names

    async def test_tool_not_in_worker_allowlist(self):
        """invalidate tool is NOT in worker allowlist (user-only)."""
        from switchboard.server.tools import WORKER_TOOL_ALLOWLIST

        assert "invalidate" not in WORKER_TOOL_ALLOWLIST


# ---------------------------------------------------------------------------
# Search integration tests: score suppression via inv_map
# ---------------------------------------------------------------------------

class TestSearchWithInvalidation:
    """Test that _handle_search applies invalidation suppression."""

    def _make_fts_msg_hit(self, msg_id, bm25=1.0, task_id=None):
        return {
            "message_id": msg_id,
            "bm25_score": bm25,
            "snippet": "snippet",
            "type": "note",
            "author": "user",
            "task_id": task_id,
            "conversation_id": "conv-1",
            "created_at": "2026-01-01T00:00:00Z",
        }

    def _make_vec_msg_hit(self, msg_id, similarity=0.9, task_id=None):
        return {
            "message_id": msg_id,
            "similarity": similarity,
            "type": "note",
            "author": "user",
            "task_id": task_id,
            "conversation_id": "conv-1",
            "created_at": "2026-01-01T00:00:00Z",
            "pinned": False,
            "title": None,
            "content": "some content",
        }

    async def test_search_suppresses_invalidated_message(self, db):
        """Search with an invalidation applies (1 - strength) multiplier to message score."""
        from switchboard.server.handlers.search import _handle_search

        fts_hit = self._make_fts_msg_hit(1, bm25=1.0)

        # Get unsuppressed score first
        with patch("switchboard.server.handlers.search.db") as mock_db, \
             patch("switchboard.server.handlers.search.emb") as mock_emb, \
             patch("switchboard.server.handlers.search._search_db") as mock_sdb:

            mock_emb.get_embedding_service.return_value.embed_safe = AsyncMock(return_value=None)
            mock_sdb.VEC_AVAILABLE = False
            mock_db.search_messages_fts = AsyncMock(return_value=[fts_hit])
            mock_db.search_tasks_fts = AsyncMock(return_value=[])
            mock_db.get_invalidations = AsyncMock(return_value=[])

            unsuppressed = await _handle_search({"query": "test"})

        # Get suppressed score (strength=0.5 → score * 0.5)
        with patch("switchboard.server.handlers.search.db") as mock_db, \
             patch("switchboard.server.handlers.search.emb") as mock_emb, \
             patch("switchboard.server.handlers.search._search_db") as mock_sdb:

            mock_emb.get_embedding_service.return_value.embed_safe = AsyncMock(return_value=None)
            mock_sdb.VEC_AVAILABLE = False
            mock_db.search_messages_fts = AsyncMock(return_value=[fts_hit])
            mock_db.search_tasks_fts = AsyncMock(return_value=[])
            mock_db.get_invalidations = AsyncMock(return_value=[
                {"entity_type": "message", "entity_id": "1", "strength": 0.5}
            ])

            suppressed = await _handle_search({"query": "test"})

        assert len(unsuppressed["results"]) == 1
        assert len(suppressed["results"]) == 1
        raw_score = unsuppressed["results"][0]["relevance_score"]
        sup_score = suppressed["results"][0]["relevance_score"]

        assert sup_score < raw_score  # suppression reduced the score
        assert sup_score > 0  # but not zeroed out
        # With strength=0.5, sup_score should be approximately raw_score * 0.5
        assert abs(sup_score - raw_score * 0.5) < 0.001

    async def test_search_with_include_invalidated_skips_suppression(self, db):
        """include_invalidated=true returns raw scores, no suppression applied."""
        from switchboard.server.handlers.search import _handle_search
        from switchboard.db.search import upsert_invalidation

        await upsert_invalidation("message", "1", 0.9)  # 90% suppression

        with patch("switchboard.server.handlers.search.db") as mock_db, \
             patch("switchboard.server.handlers.search.emb") as mock_emb, \
             patch("switchboard.server.handlers.search._search_db") as mock_sdb:

            mock_emb.get_embedding_service.return_value.embed_safe = AsyncMock(return_value=None)
            mock_sdb.VEC_AVAILABLE = False
            mock_db.search_messages_fts = AsyncMock(return_value=[self._make_fts_msg_hit(1, bm25=1.0)])
            mock_db.search_tasks_fts = AsyncMock(return_value=[])
            # get_invalidations should NOT be called when include_invalidated=True
            mock_db.get_invalidations = AsyncMock(return_value=[])

            result = await _handle_search({"query": "test", "include_invalidated": True})

        mock_db.get_invalidations.assert_not_awaited()
        results = result["results"]
        assert len(results) == 1
        # Score should be positive (not zeroed) — not suppressed.
        # Exact value depends on recency decay for the mock created_at date.
        assert results[0]["relevance_score"] > 0

    async def test_search_without_invalidation_loads_inv_map(self, db):
        """include_invalidated=false (default) calls get_invalidations."""
        from switchboard.server.handlers.search import _handle_search

        with patch("switchboard.server.handlers.search.db") as mock_db, \
             patch("switchboard.server.handlers.search.emb") as mock_emb, \
             patch("switchboard.server.handlers.search._search_db") as mock_sdb:

            mock_emb.get_embedding_service.return_value.embed_safe = AsyncMock(return_value=None)
            mock_sdb.VEC_AVAILABLE = False
            mock_db.search_messages_fts = AsyncMock(return_value=[])
            mock_db.search_tasks_fts = AsyncMock(return_value=[])
            mock_db.get_invalidations = AsyncMock(return_value=[])

            await _handle_search({"query": "test"})

        mock_db.get_invalidations.assert_awaited_once()

    async def test_include_invalidated_param_in_search_tool_schema(self):
        """Search tool schema includes include_invalidated boolean param."""
        from switchboard.server.tools import SEARCH_TOOLS

        search_tool = SEARCH_TOOLS[0]
        props = search_tool.inputSchema["properties"]
        assert "include_invalidated" in props
        assert props["include_invalidated"]["type"] == "boolean"
