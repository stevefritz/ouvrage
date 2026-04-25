"""Tests for the unified `search` MCP tool.

Covers:
- Returns error when embedding fails (no OPENAI_API_KEY)
- Searches task goals, task messages, and message chunks
- Returns compact result cards (type, entity_id, snippet, relevance_score, ...)
- De-duplicates message hits covered by chunk hits
- Optional project_id scopes results
- Limit parameter respected (max 30)
- Results ordered by best relevance score
"""

import asyncio
import struct
from unittest.mock import AsyncMock, patch

import pytest

from ouvrage.server.handlers.search import _handle_search, _handle_set_weight


def _encode_vector(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _unit_vec(dim: int, index: int) -> list[float]:
    v = [0.0] * dim
    v[index % dim] = 1.0
    return v


# ---------------------------------------------------------------------------
# Error path: embedding fails
# ---------------------------------------------------------------------------

class TestSearchEmbedError:
    async def test_falls_back_to_fts_when_embed_fails(self, db, sample_project):
        """When embedding fails, search falls back to FTS-only and returns results (not an error)."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService

        class FailService(EmbeddingService):
            async def embed(self, text):
                raise RuntimeError("no key")

        set_embedding_service(FailService())
        try:
            result = await _handle_search({"query": "anything"})
            # Should return results dict (not an error), possibly empty
            assert "results" in result
            assert "total_candidates" in result
            assert "error" not in result
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Task results — returns compact result cards with entity_id, snippet, etc.
# ---------------------------------------------------------------------------

class TestSearchTaskResults:
    async def test_task_card_returned(self, db, sample_project):
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        task = await db.create_task(
            id="test-project/search-task-goal",
            project_id="test-project",
            goal="Implement the authentication module",
        )
        await db.set_task_embedding(task["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "authentication", "limit": 10})
            assert "results" in result
            entity_ids = [r["entity_id"] for r in result["results"]]
            assert task["id"] in entity_ids
        finally:
            set_embedding_service(None)

    async def test_task_result_has_compact_fields(self, db, sample_project):
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 1)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        task = await db.create_task(
            id="test-project/fields-task",
            project_id="test-project",
            goal="Fix the caching bug",
        )
        await db.set_task_embedding(task["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "cache"})
            matching = [r for r in result["results"] if r["entity_id"] == task["id"]]
            assert len(matching) == 1
            r = matching[0]
            assert r["type"] == "task"
            assert r["entity_id"] == task["id"]
            assert "snippet" in r
            assert "relevance_score" in r
            assert r["author"] is None
            assert r["message_type"] is None
        finally:
            set_embedding_service(None)

    async def test_task_snippet_is_from_goal(self, db, sample_project):
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 2)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        task = await db.create_task(
            id="test-project/snippet-task",
            project_id="test-project",
            goal="Fix the caching bug in production",
        )
        await db.set_task_embedding(task["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "cache"})
            matching = [r for r in result["results"] if r["entity_id"] == task["id"]]
            assert len(matching) == 1
            # Snippet should be derived from the goal
            assert "Fix the caching bug" in matching[0]["snippet"]
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Task message results — returned as task_message cards
# ---------------------------------------------------------------------------

class TestSearchMessageResults:
    async def test_task_message_returns_message_card(self, db, sample_project):
        """A match in a task message returns a task_message card, not a task object."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 3)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        task = await db.create_task(
            id="test-project/msg-task",
            project_id="test-project",
            goal="Build the pipeline",
        )
        msg = await db.post_task_message(
            task_id=task["id"],
            author="cc-worker",
            content="Completed the data pipeline implementation with error handling.",
            type="result",
        )
        await db.set_message_embedding(msg["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "pipeline result"})
            # Should find the message card
            msg_cards = [r for r in result["results"] if r["type"] == "task_message"]
            msg_ids = [r["entity_id"] for r in msg_cards]
            assert str(msg["id"]) in msg_ids
        finally:
            set_embedding_service(None)

    async def test_message_card_has_author_and_message_type(self, db, sample_project):
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        task = await db.create_task(
            id="test-project/msg-fields-task",
            project_id="test-project",
            goal="Build something",
        )
        msg = await db.post_task_message(
            task_id=task["id"],
            author="cc-worker",
            content="Completed implementation with full test coverage.",
            type="result",
        )
        await db.set_message_embedding(msg["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "implementation"})
            msg_cards = [r for r in result["results"] if r.get("entity_id") == str(msg["id"])]
            assert len(msg_cards) == 1
            card = msg_cards[0]
            assert card["author"] == "cc-worker"
            assert card["message_type"] == "result"
            assert "snippet" in card
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Snippet truncation
# ---------------------------------------------------------------------------

class TestSearchSnippet:
    async def test_snippet_max_200_chars(self, db, sample_project):
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        long_content = "This is a very long message. " * 20  # 580 chars
        task = await db.create_task(
            id="test-project/long-msg-task",
            project_id="test-project",
            goal="Task with long message",
        )
        msg = await db.post_task_message(
            task_id=task["id"],
            author="human",
            content=long_content,
            type="note",
        )
        await db.set_message_embedding(msg["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "long message"})
            for r in result["results"]:
                assert len(r["snippet"]) <= 201  # 200 chars + possible ellipsis char
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# project_id scoping
# ---------------------------------------------------------------------------

class TestSearchProjectScoping:
    async def test_project_id_filters_task_results(self, db, sample_project):
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        await db.create_project(id="other-proj", repo="https://github.com/x/y.git", working_dir="/work/y", default_branch="main")
        task_mine = await db.create_task(
            id="test-project/scoped-mine",
            project_id="test-project",
            goal="My scoped task",
        )
        task_other = await db.create_task(
            id="other-proj/scoped-other",
            project_id="other-proj",
            goal="Other project task",
        )
        blob = encode_vector(vec)
        await db.set_task_embedding(task_mine["id"], blob)
        await db.set_task_embedding(task_other["id"], blob)

        set_embedding_service(MockService())
        try:
            result = await _handle_search({
                "query": "scoped task",
                "project_id": "test-project",
                "limit": 10,
            })
            entity_ids = [r["entity_id"] for r in result["results"]]
            assert task_mine["id"] in entity_ids
            assert task_other["id"] not in entity_ids
        finally:
            set_embedding_service(None)

    async def test_project_id_filters_message_results(self, db, sample_project):
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 1)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        await db.create_project(id="other-proj2", repo="https://github.com/x/z.git", working_dir="/work/z", default_branch="main")

        task_mine = await db.create_task(
            id="test-project/scoped-msg-mine",
            project_id="test-project",
            goal="My task with message",
        )
        task_other = await db.create_task(
            id="other-proj2/scoped-msg-other",
            project_id="other-proj2",
            goal="Other task with message",
        )

        msg_mine = await db.post_task_message(
            task_id=task_mine["id"],
            author="human",
            content="This message is in test-project and has plenty of content to embed.",
            type="note",
        )
        msg_other = await db.post_task_message(
            task_id=task_other["id"],
            author="human",
            content="This message is in other-proj2 and has plenty of content to embed.",
            type="note",
        )
        blob = encode_vector(vec)
        await db.set_message_embedding(msg_mine["id"], blob)
        await db.set_message_embedding(msg_other["id"], blob)

        set_embedding_service(MockService())
        try:
            result = await _handle_search({
                "query": "message content",
                "project_id": "test-project",
                "limit": 10,
            })
            entity_ids = [r["entity_id"] for r in result["results"]]
            assert str(msg_mine["id"]) in entity_ids
            assert str(msg_other["id"]) not in entity_ids
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Limit parameter
# ---------------------------------------------------------------------------

class TestSearchLimit:
    async def test_limit_capped_at_30(self, db, sample_project):
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        # Create 35 tasks with embeddings
        for i in range(35):
            t = await db.create_task(
                id=f"test-project/limit-task-{i}",
                project_id="test-project",
                goal=f"Task number {i} for limit testing",
            )
            await db.set_task_embedding(t["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "task limit", "limit": 50})
            assert len(result["results"]) <= 30
        finally:
            set_embedding_service(None)

    async def test_default_limit_is_10(self, db, sample_project):
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        for i in range(15):
            t = await db.create_task(
                id=f"test-project/default-limit-{i}",
                project_id="test-project",
                goal=f"Default limit task {i}",
            )
            await db.set_task_embedding(t["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "default limit"})
            assert len(result["results"]) <= 10
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Results are ranked by best relevance score
# ---------------------------------------------------------------------------

class TestSearchRanking:
    async def test_results_sorted_by_relevance_descending(self, db, sample_project):
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        query_vec = _unit_vec(4, 0)
        other_vec = _unit_vec(4, 1)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return query_vec

        # Task with high similarity (same vector as query)
        task_high = await db.create_task(
            id="test-project/rank-high",
            project_id="test-project",
            goal="High relevance task",
        )
        # Task with lower similarity (orthogonal vector)
        task_low = await db.create_task(
            id="test-project/rank-low",
            project_id="test-project",
            goal="Low relevance task",
        )
        await db.set_task_embedding(task_high["id"], encode_vector(query_vec))
        await db.set_task_embedding(task_low["id"], encode_vector(other_vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "relevance", "limit": 10})
            entity_ids = [r["entity_id"] for r in result["results"]]
            # High relevance task should appear before low relevance task
            assert task_high["id"] in entity_ids
            assert entity_ids.index(task_high["id"]) < entity_ids.index(task_low["id"])
        finally:
            set_embedding_service(None)

    async def test_total_candidates_in_response(self, db, sample_project):
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService

        class MockService(EmbeddingService):
            async def embed(self, text):
                return _unit_vec(4, 0)

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "anything"})
            assert "total_candidates" in result
            assert isinstance(result["total_candidates"], int)
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# set_weight MCP tool
# ---------------------------------------------------------------------------

class TestSetWeight:
    async def test_tool_registered_in_dispatch(self):
        from ouvrage.server.dispatch import TOOL_HANDLERS
        assert "set_weight" in TOOL_HANDLERS

    async def test_tool_schema_in_search_tools(self):
        from ouvrage.server.tools import SEARCH_TOOLS
        names = [t.name for t in SEARCH_TOOLS]
        assert "set_weight" in names

    async def test_set_weight_returns_row(self, db):
        row = await _handle_set_weight({
            "entity_type": "task",
            "entity_id": "my-task-1",
            "weight": 2.0,
        })
        assert row["entity_type"] == "task"
        assert row["entity_id"] == "my-task-1"
        assert row["weight"] == 2.0
        assert row["reason"] is None

    async def test_set_weight_with_reason(self, db):
        row = await _handle_set_weight({
            "entity_type": "message",
            "entity_id": "42",
            "weight": 0.5,
            "reason": "noisy result",
        })
        assert row["weight"] == 0.5
        assert row["reason"] == "noisy result"

    async def test_set_weight_upserts(self, db):
        await _handle_set_weight({"entity_type": "task", "entity_id": "t-1", "weight": 1.0})
        row = await _handle_set_weight({"entity_type": "task", "entity_id": "t-1", "weight": 2.5})
        assert row["weight"] == 2.5

    async def test_invalid_entity_type_raises(self, db):
        with pytest.raises(ValueError, match="Invalid entity_type"):
            await _handle_set_weight({
                "entity_type": "project",
                "entity_id": "p-1",
                "weight": 1.0,
            })

    async def test_weight_below_range_raises(self, db):
        with pytest.raises(ValueError, match="out of range"):
            await _handle_set_weight({
                "entity_type": "task",
                "entity_id": "t-1",
                "weight": -0.1,
            })

    async def test_weight_above_range_raises(self, db):
        with pytest.raises(ValueError, match="out of range"):
            await _handle_set_weight({
                "entity_type": "chunk",
                "entity_id": "c-1",
                "weight": 3.1,
            })


# ---------------------------------------------------------------------------
# Manual weight integration — weights applied in scoring loop
# ---------------------------------------------------------------------------

class TestSearchManualWeights:
    async def test_high_weight_boosts_result_above_unweighted(self, db, sample_project):
        """A low-similarity task with a high manual weight ranks above a high-similarity unweighted task."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector
        import ouvrage.db.search_weights as sw_db

        query_vec = _unit_vec(4, 0)
        other_vec = _unit_vec(4, 1)  # orthogonal → similarity 0

        class MockService(EmbeddingService):
            async def embed(self, text):
                return query_vec

        task_high_sim = await db.create_task(
            id="test-project/weight-high-sim",
            project_id="test-project",
            goal="High similarity task",
        )
        task_low_sim = await db.create_task(
            id="test-project/weight-low-sim",
            project_id="test-project",
            goal="Low similarity task",
        )
        await db.set_task_embedding(task_high_sim["id"], encode_vector(query_vec))
        await db.set_task_embedding(task_low_sim["id"], encode_vector(other_vec))

        # Give the low-similarity task a 3x weight boost
        await sw_db.set_weight("task", task_low_sim["id"], 3.0)

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "task", "limit": 10})
            entity_ids = [r["entity_id"] for r in result["results"]]
            assert task_low_sim["id"] in entity_ids
            assert task_high_sim["id"] in entity_ids
            assert entity_ids.index(task_low_sim["id"]) < entity_ids.index(task_high_sim["id"])
        finally:
            set_embedding_service(None)
            await sw_db.remove_weight("task", task_low_sim["id"])

    async def test_zero_weight_sinks_result(self, db, sample_project):
        """A task with weight 0.0 scores 0 and ranks last."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector
        import ouvrage.db.search_weights as sw_db

        query_vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return query_vec

        # High-similarity task with weight 0.0
        task_zeroed = await db.create_task(
            id="test-project/weight-zero",
            project_id="test-project",
            goal="Zeroed out task",
        )
        # Normal task with default weight (1.0)
        task_normal = await db.create_task(
            id="test-project/weight-normal",
            project_id="test-project",
            goal="Normal weight task",
        )
        await db.set_task_embedding(task_zeroed["id"], encode_vector(query_vec))
        await db.set_task_embedding(task_normal["id"], encode_vector(query_vec))

        await sw_db.set_weight("task", task_zeroed["id"], 0.0)

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "task", "limit": 10})
            zeroed_cards = [r for r in result["results"] if r["entity_id"] == task_zeroed["id"]]
            normal_cards = [r for r in result["results"] if r["entity_id"] == task_normal["id"]]
            assert len(zeroed_cards) == 1
            assert len(normal_cards) == 1
            # Zero-weighted task must have score 0
            assert zeroed_cards[0]["relevance_score"] == 0.0
            # And rank below the normal task
            entity_ids = [r["entity_id"] for r in result["results"]]
            assert entity_ids.index(task_normal["id"]) < entity_ids.index(task_zeroed["id"])
        finally:
            set_embedding_service(None)
            await sw_db.remove_weight("task", task_zeroed["id"])

    async def test_anchor_not_affected_by_weights(self, db, sample_project):
        """Anchor is the newest created_at regardless of weights; a weighted old task can outscore a new one."""
        from ouvrage.embeddings.service import set_embedding_service, EmbeddingService, encode_vector
        import ouvrage.db.search_weights as sw_db

        query_vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return query_vec

        # Both tasks have the same similarity; one is older and gets a high weight
        task_new = await db.create_task(
            id="test-project/anchor-new",
            project_id="test-project",
            goal="Newly created task",
        )
        task_old = await db.create_task(
            id="test-project/anchor-old",
            project_id="test-project",
            goal="Older task with boost",
        )
        await db.set_task_embedding(task_new["id"], encode_vector(query_vec))
        await db.set_task_embedding(task_old["id"], encode_vector(query_vec))

        # Give the old task a weight boost — it should be able to rank above the new task
        await sw_db.set_weight("task", task_old["id"], 3.0)

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "task", "limit": 10})
            new_cards = [r for r in result["results"] if r["entity_id"] == task_new["id"]]
            old_cards = [r for r in result["results"] if r["entity_id"] == task_old["id"]]
            assert len(new_cards) == 1
            assert len(old_cards) == 1
            # New task (anchor) gets recency_mult=1.0 — verify it has a positive score
            assert new_cards[0]["relevance_score"] > 0.0
            # Old task with 3x weight should outscore the new unweighted task
            assert old_cards[0]["relevance_score"] > new_cards[0]["relevance_score"]
        finally:
            set_embedding_service(None)
            await sw_db.remove_weight("task", task_old["id"])
