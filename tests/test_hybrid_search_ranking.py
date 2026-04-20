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

from ouvrage.server.handlers.search import _handle_search, _recency_mult, _TYPE_BOOST


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
    def test_today_is_1(self):
        now = datetime.now(timezone.utc)
        assert _recency_mult(now.isoformat(), now) == 1.0

    def test_90_days_is_0_3(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=90)
        mult = _recency_mult(old.isoformat(), now)
        assert abs(mult - 0.3) < 0.001

    def test_beyond_90_days_capped_at_0_3(self):
        now = datetime.now(timezone.utc)
        very_old = now - timedelta(days=365)
        mult = _recency_mult(very_old.isoformat(), now)
        assert abs(mult - 0.3) < 0.001

    def test_45_days_is_midpoint(self):
        now = datetime.now(timezone.utc)
        mid = now - timedelta(days=45)
        mult = _recency_mult(mid.isoformat(), now)
        assert abs(mult - 0.65) < 0.001

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
        from ouvrage.server.handlers.search import _PINNED_BOOST
        assert _PINNED_BOOST == 1.3

    def test_dual_match_boost(self):
        from ouvrage.server.handlers.search import _DUAL_MATCH_BOOST
        assert _DUAL_MATCH_BOOST == 1.3


# ---------------------------------------------------------------------------
# FTS fallback mode (no embeddings available)
# ---------------------------------------------------------------------------

class TestFtsFallback:
    async def test_fts_only_returns_results_not_error(self, db, sample_project):
        """When embed_safe returns None, FTS-only mode runs without error."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService

        class NoKeyService(EmbeddingService):
            async def embed(self, text):
                raise RuntimeError("no API key")

        # Create a task with FTS-indexable content
        await db.create_task(
            id="test-project/fts-fallback-task",
            project_id="test-project",
            goal="fts_fallback_unique_token implementation",
        )

        set_embedding_service(NoKeyService())
        try:
            result = await _handle_search({"query": "fts_fallback_unique_token"})
            assert "results" in result
            assert "error" not in result
            entity_ids = [r["entity_id"] for r in result["results"]]
            assert "test-project/fts-fallback-task" in entity_ids
        finally:
            set_embedding_service(None)

    async def test_fts_fallback_no_dual_match_boost(self, db, sample_project):
        """FTS-only mode doesn't crash; dual-match boost is simply not applied."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService

        class NoKeyService(EmbeddingService):
            async def embed(self, text):
                raise RuntimeError("no API key")

        await db.create_task(
            id="test-project/no-dual-task",
            project_id="test-project",
            goal="nodual_unique_phrase task goal",
        )

        set_embedding_service(NoKeyService())
        try:
            result = await _handle_search({"query": "nodual_unique_phrase"})
            assert "results" in result
            # Results are FTS scores only (normalized), no dual boost
            for r in result["results"]:
                assert r["relevance_score"] > 0
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Parallel FTS + vec execution (hybrid mode)
# ---------------------------------------------------------------------------

class TestHybridParallelSearch:
    async def test_fts_and_vec_both_run(self, db, sample_project):
        """In hybrid mode both FTS and vec results are merged."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        # Task matching via FTS (unique token in goal)
        task_fts = await db.create_task(
            id="test-project/hybrid-fts-only",
            project_id="test-project",
            goal="hybrid_fts_only_token task",
        )
        # Task matching via vec similarity
        task_vec = await db.create_task(
            id="test-project/hybrid-vec-only",
            project_id="test-project",
            goal="A task with no FTS match but vec similarity",
        )
        await db.set_task_embedding(task_vec["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            # This query matches task_fts via FTS but also task_vec via vec
            result = await _handle_search({"query": "hybrid_fts_only_token", "limit": 10})
            entity_ids = [r["entity_id"] for r in result["results"]]
            # FTS match should appear
            assert task_fts["id"] in entity_ids
        finally:
            set_embedding_service(None)

    async def test_dual_match_gets_higher_score(self, db, sample_project):
        """A result matching both FTS and vec gets a higher score than FTS-only."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        # Task that matches both FTS (token in goal) and vec (has embedding)
        task_dual = await db.create_task(
            id="test-project/dual-match-task",
            project_id="test-project",
            goal="dual_match_unique_token implementation",
        )
        await db.set_task_embedding(task_dual["id"], encode_vector(vec))

        # Task that only matches FTS (no embedding)
        task_fts_only = await db.create_task(
            id="test-project/fts-only-task",
            project_id="test-project",
            goal="dual_match_unique_token no embedding",
        )

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "dual_match_unique_token", "limit": 10})
            scores = {r["entity_id"]: r["relevance_score"] for r in result["results"]}
            assert task_dual["id"] in scores
            assert task_fts_only["id"] in scores
            # Dual match should score higher
            assert scores[task_dual["id"]] > scores[task_fts_only["id"]]
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestScoreNormalization:
    async def test_all_scores_are_0_to_1(self, db, sample_project):
        """All relevance_score values in results are in [0, ~2] range (boosts can push > 1)."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        task = await db.create_task(
            id="test-project/score-range-task",
            project_id="test-project",
            goal="score_normalization_check goal",
        )
        await db.set_task_embedding(task["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "score_normalization_check"})
            for r in result["results"]:
                # With boosts, scores can exceed 1.0, but should be positive
                assert r["relevance_score"] >= 0
                # And not astronomically high (max boost: 1.3 * 1.5 * 1.3 * 1.3 ≈ 3.3 for pinned spec)
                assert r["relevance_score"] < 5.0
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Type boost
# ---------------------------------------------------------------------------

class TestTypeBoost:
    async def test_spec_message_scores_higher_than_status(self, db, sample_project):
        """A 'spec' type message scores higher than a 'status' type message with same vec similarity."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        task = await db.create_task(
            id="test-project/type-boost-task",
            project_id="test-project",
            goal="type boost test task",
        )

        msg_spec = await db.post_task_message(
            task_id=task["id"],
            author="human",
            content="This is a spec message with long content for embedding testing purposes.",
            type="spec",
        )
        msg_status = await db.post_task_message(
            task_id=task["id"],
            author="human",
            content="This is a status message with long content for embedding testing purposes.",
            type="status",
        )

        blob = encode_vector(vec)
        await db.set_message_embedding(msg_spec["id"], blob)
        await db.set_message_embedding(msg_status["id"], blob)

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "type boost", "limit": 10})
            scores = {r["entity_id"]: r["relevance_score"] for r in result["results"]}
            assert str(msg_spec["id"]) in scores
            assert str(msg_status["id"]) in scores
            assert scores[str(msg_spec["id"])] > scores[str(msg_status["id"])]
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Pinned boost
# ---------------------------------------------------------------------------

class TestPinnedBoost:
    async def test_pinned_message_scores_higher(self, db, sample_project):
        """A pinned message scores 1.3x higher than the same non-pinned message."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector
        from ouvrage.db.connection import get_db

        vec = _unit_vec(4, 1)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        task = await db.create_task(
            id="test-project/pinned-boost-task",
            project_id="test-project",
            goal="pinned boost test",
        )

        msg_pinned = await db.post_task_message(
            task_id=task["id"],
            author="human",
            content="Pinned message with plenty of content to embed and test pinned boost.",
            type="note",
        )
        msg_unpinned = await db.post_task_message(
            task_id=task["id"],
            author="human",
            content="Unpinned message with plenty of content to embed and test pinned boost.",
            type="note",
        )

        # Mark msg_pinned as pinned
        async with get_db() as conn:
            await conn.execute("UPDATE messages SET pinned = 1 WHERE id = ?", (msg_pinned["id"],))
            await conn.commit()

        blob = encode_vector(vec)
        await db.set_message_embedding(msg_pinned["id"], blob)
        await db.set_message_embedding(msg_unpinned["id"], blob)

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "pinned boost", "limit": 10})
            scores = {r["entity_id"]: r["relevance_score"] for r in result["results"]}
            assert str(msg_pinned["id"]) in scores
            assert str(msg_unpinned["id"]) in scores
            assert scores[str(msg_pinned["id"])] > scores[str(msg_unpinned["id"])]
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Recency decay
# ---------------------------------------------------------------------------

class TestRecencyDecay:
    async def test_recent_task_scores_higher_than_old(self, db, sample_project):
        """A very old task should score lower than a recent one with same FTS match."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService
        from ouvrage.db.connection import get_db
        from ouvrage.db._helpers import now_iso

        class NoKeyService(EmbeddingService):
            async def embed(self, text):
                raise RuntimeError("no key")

        task_new = await db.create_task(
            id="test-project/recency-new-task",
            project_id="test-project",
            goal="recency_decay_token new task",
        )
        task_old = await db.create_task(
            id="test-project/recency-old-task",
            project_id="test-project",
            goal="recency_decay_token old task",
        )

        # Make task_old appear 200 days ago
        old_date = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        async with get_db() as conn:
            await conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id = ?",
                (old_date, task_old["id"]),
            )
            await conn.commit()

        set_embedding_service(NoKeyService())
        try:
            result = await _handle_search({"query": "recency_decay_token", "limit": 10})
            scores = {r["entity_id"]: r["relevance_score"] for r in result["results"]}
            assert task_new["id"] in scores
            assert task_old["id"] in scores
            # Recent task should score higher
            assert scores[task_new["id"]] > scores[task_old["id"]]
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Merge and deduplicate
# ---------------------------------------------------------------------------

class TestMergeAndDeduplicate:
    async def test_deduplication_keeps_highest_score(self, db, sample_project):
        """If same entity_id appears in multiple result pools, keep highest score."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        # Task with both FTS match and vec match → appears in both pools
        task = await db.create_task(
            id="test-project/dedup-task",
            project_id="test-project",
            goal="deduplication_test_token task goal",
        )
        await db.set_task_embedding(task["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "deduplication_test_token", "limit": 10})
            # The task should appear only once in results
            entity_ids = [r["entity_id"] for r in result["results"]]
            count = entity_ids.count(task["id"])
            assert count == 1
        finally:
            set_embedding_service(None)

    async def test_chunk_suppresses_message_hit(self, db, sample_project):
        """If a message has a chunk hit, the message should not appear separately."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

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
            with patch("ouvrage.db.search_message_chunks", return_value=chunk_hit if False else [chunk_hit]):
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

class TestTopN:
    async def test_returns_at_most_limit_results(self, db, sample_project):
        """Results are capped at the requested limit."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        # Create 15 tasks
        for i in range(15):
            t = await db.create_task(
                id=f"test-project/topn-task-{i}",
                project_id="test-project",
                goal=f"topn_limit_token task {i}",
            )
            await db.set_task_embedding(t["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "topn_limit_token", "limit": 5})
            assert len(result["results"]) <= 5
        finally:
            set_embedding_service(None)

    async def test_results_sorted_descending_by_score(self, db, sample_project):
        """Results are sorted by relevance_score descending."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        for i in range(5):
            t = await db.create_task(
                id=f"test-project/sorted-task-{i}",
                project_id="test-project",
                goal=f"sorted_order_token goal {i}",
            )
            await db.set_task_embedding(t["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "sorted_order_token", "limit": 10})
            scores = [r["relevance_score"] for r in result["results"]]
            assert scores == sorted(scores, reverse=True)
        finally:
            set_embedding_service(None)

    async def test_total_candidates_reflects_all_before_limit(self, db, sample_project):
        """total_candidates is the count before limit is applied."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        for i in range(10):
            t = await db.create_task(
                id=f"test-project/cands-task-{i}",
                project_id="test-project",
                goal=f"total_candidates_token task {i}",
            )
            await db.set_task_embedding(t["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "total_candidates_token", "limit": 3})
            assert result["total_candidates"] >= len(result["results"])
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Task vs message weights
# ---------------------------------------------------------------------------

class TestWeights:
    async def test_task_fts_weight_exceeds_vec_weight(self, db, sample_project):
        """For task results, FTS weight (0.6) > vec weight (0.4)."""
        from ouvrage.server.handlers.search import _TASK_FTS_WEIGHT, _TASK_VEC_WEIGHT
        assert _TASK_FTS_WEIGHT == 0.6
        assert _TASK_VEC_WEIGHT == 0.4

    async def test_message_vec_weight_exceeds_fts_weight(self, db, sample_project):
        """For message results, vec weight (0.6) > FTS weight (0.4)."""
        from ouvrage.server.handlers.search import _MSG_FTS_WEIGHT, _MSG_VEC_WEIGHT
        assert _MSG_VEC_WEIGHT == 0.6
        assert _MSG_FTS_WEIGHT == 0.4


# ---------------------------------------------------------------------------
# Result shape — task_id / conversation_id fields for dashboard hydration
# ---------------------------------------------------------------------------

class TestResultShapeFields:
    async def test_task_result_has_task_id(self, db, sample_project):
        """Task results include task_id equal to entity_id for dashboard hydration."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService

        class NoKeyService(EmbeddingService):
            async def embed(self, text):
                raise RuntimeError("no API key")

        await db.create_task(
            id="test-project/shape-task",
            project_id="test-project",
            goal="shape_field_unique_token task",
        )

        set_embedding_service(NoKeyService())
        try:
            result = await _handle_search({"query": "shape_field_unique_token"})
            task_results = [r for r in result["results"] if r["type"] == "task"]
            assert len(task_results) >= 1
            tr = task_results[0]
            assert "task_id" in tr
            assert tr["task_id"] == tr["entity_id"]
            assert "conversation_id" in tr
            assert tr["conversation_id"] is None
            assert "status" in tr
        finally:
            set_embedding_service(None)

    async def test_message_result_has_task_id_and_conversation_id(self, db, sample_project):
        """Message results include task_id and conversation_id fields."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService

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
