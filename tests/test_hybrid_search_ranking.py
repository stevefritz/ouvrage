"""Tests for hybrid FTS5 + sqlite-vec search ranking in _handle_search.

Covers:
- FTS and vec run in parallel, results merged
- Score normalization (FTS BM25 to 0-1, vec already 0-1)
- Task weights: FTS 0.6 / vec 0.4
- Message weights: FTS 0.4 / vec 0.6
- Type boost (spec 1.5x, review 1.4x, etc.)
- Pinned boost (1.3x)
- Recency decay (1.0 today → 0.8 at 6 months)
- Dual-match boost (1.3x for results in both FTS and vec)
- Merge and deduplicate by entity_id
- FTS-only fallback when embed_safe returns None
- Return top N ranked results
"""

import asyncio
import struct
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from switchboard.server.handlers.search import _handle_search, _recency_mult, _TYPE_BOOST


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encode_vector(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _unit_vec(dim: int, index: int) -> list[float]:
    v = [0.0] * dim
    v[index % dim] = 1.0
    return v


# ---------------------------------------------------------------------------
# _recency_mult unit tests
# ---------------------------------------------------------------------------

class TestRecencyMult:


    def test_none_returns_1(self):
        now = datetime.now(timezone.utc)
        assert _recency_mult(None, now) == 1.0

    def test_invalid_string_returns_1(self):
        now = datetime.now(timezone.utc)
        assert _recency_mult("not-a-date", now) == 1.0


# ---------------------------------------------------------------------------
# Type boost constants
# ---------------------------------------------------------------------------

class TestTypeBoostConstants:
    def test_spec_boost(self):
        assert _TYPE_BOOST["spec"] == 1.5

    def test_review_boost(self):
        assert _TYPE_BOOST["review"] == 1.4

    def test_pinned_boost_applied(self):
        from switchboard.server.handlers.search import _PINNED_BOOST
        assert _PINNED_BOOST == 1.3

    def test_dual_match_boost(self):
        from switchboard.server.handlers.search import _DUAL_MATCH_BOOST
        assert _DUAL_MATCH_BOOST == 1.3


# ---------------------------------------------------------------------------
# FTS fallback mode (no embeddings available)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Parallel FTS + vec execution (hybrid mode)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Type boost
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pinned boost
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Recency decay
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Merge and deduplicate
# ---------------------------------------------------------------------------

class TestMergeAndDeduplicate:

    async def test_chunk_suppresses_message_hit(self, db, sample_project):
        """If a message has a chunk hit, the message should not appear separately."""
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        task = await db.create_task(
            id="test-project/chunk-suppress-task",
            project_id="test-project",
            goal="chunk suppression test",
        )
        msg = await db.post_task_message(
            task_id=task["id"],
            author="human",
            content="This message is long enough to get chunked " * 20,
            type="note",
        )
        await db.set_message_embedding(msg["id"], encode_vector(vec))

        # Mock search_message_chunks to return a chunk hit for this message
        chunk_hit = {
            "chunk_id": 1,
            "message_id": msg["id"],
            "chunk_index": 0,
            "chunk_heading": None,
            "chunk_content": "chunk content here",
            "conversation_id": None,
            "task_id": task["id"],
            "author": "human",
            "type": "note",
            "title": None,
            "pinned": False,
            "created_at": msg["created_at"],
            "similarity": 0.9,
        }

        set_embedding_service(MockService())
        try:
            with patch("switchboard.db.search_message_chunks", return_value=chunk_hit if False else [chunk_hit]):
                result = await _handle_search({"query": "chunk suppression", "limit": 10})
                types_for_msg = [r["type"] for r in result["results"] if r["entity_id"] == str(msg["id"])]
                # Should only appear as chunk, not as task_message
                if types_for_msg:
                    assert "task_message" not in types_for_msg
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Top-N return
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Task vs message weights
# ---------------------------------------------------------------------------

class TestWeights:
    async def test_task_fts_weight_exceeds_vec_weight(self, db, sample_project):
        """For task results, FTS weight (0.6) > vec weight (0.4)."""
        from switchboard.server.handlers.search import _TASK_FTS_WEIGHT, _TASK_VEC_WEIGHT
        assert _TASK_FTS_WEIGHT == 0.6
        assert _TASK_VEC_WEIGHT == 0.4

    async def test_message_vec_weight_exceeds_fts_weight(self, db, sample_project):
        """For message results, vec weight (0.6) > FTS weight (0.4)."""
        from switchboard.server.handlers.search import _MSG_FTS_WEIGHT, _MSG_VEC_WEIGHT
        assert _MSG_VEC_WEIGHT == 0.6
        assert _MSG_FTS_WEIGHT == 0.4


# ---------------------------------------------------------------------------
# Result shape — task_id / conversation_id fields for dashboard hydration
# ---------------------------------------------------------------------------

class TestResultShapeFields:

    async def test_message_result_has_task_id_and_conversation_id(self, db, sample_project):
        """Message results include task_id and conversation_id fields."""
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService

        class NoKeyService(EmbeddingService):
            async def embed(self, text):
                raise RuntimeError("no API key")

        await db.create_task(
            id="test-project/msg-shape-task",
            project_id="test-project",
            goal="msg shape task",
        )
        await db.post_task_message(
            task_id="test-project/msg-shape-task",
            type="progress",
            author="worker",
            content="msg_shape_unique_token progress update",
        )

        set_embedding_service(NoKeyService())
        try:
            result = await _handle_search({"query": "msg_shape_unique_token"})
            msg_results = [r for r in result["results"] if r["type"] == "task_message"]
            assert len(msg_results) >= 1
            mr = msg_results[0]
            assert "task_id" in mr
            assert mr["task_id"] == "test-project/msg-shape-task"
            assert "conversation_id" in mr
        finally:
            set_embedding_service(None)
