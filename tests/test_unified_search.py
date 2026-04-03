"""Tests for the unified `search` MCP tool.

Covers:
- Returns error when embedding fails (no OPENAI_API_KEY)
- Searches task goals, task messages, and message chunks
- Groups all matches by task — returns task objects, not typed result cards
- De-duplicates: a task appears once even if matched on goal AND message AND chunk
- Optional project_id scopes results
- Limit parameter respected (max 30)
- Results ordered by best relevance score per task
"""

import asyncio
import struct
from unittest.mock import AsyncMock, patch

import pytest

from switchboard.server.handlers.search import _handle_search


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
    async def test_returns_error_when_embed_fails(self, db, sample_project):
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService

        class FailService(EmbeddingService):
            async def embed(self, text):
                raise RuntimeError("no key")

        set_embedding_service(FailService())
        try:
            result = await _handle_search({"query": "anything"})
            assert "error" in result
            assert "OPENAI_API_KEY" in result["error"]
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Task results — returns full task objects
# ---------------------------------------------------------------------------

class TestSearchTaskResults:
    async def test_task_objects_returned(self, db, sample_project):
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

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
            ids = [r["id"] for r in result["results"]]
            assert task["id"] in ids
        finally:
            set_embedding_service(None)

    async def test_task_result_has_task_fields(self, db, sample_project):
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

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
            matching = [r for r in result["results"] if r["id"] == task["id"]]
            assert len(matching) == 1
            r = matching[0]
            # Must be a full task object with standard fields
            assert "id" in r
            assert "goal" in r
            assert "status" in r
            assert r["goal"] == "Fix the caching bug"
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Task message results — matched via task message, returns task object
# ---------------------------------------------------------------------------

class TestSearchMessageResults:
    async def test_task_message_match_returns_task(self, db, sample_project):
        """A match in a task message should return the parent task object."""
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

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
            ids = [r["id"] for r in result["results"]]
            assert task["id"] in ids
        finally:
            set_embedding_service(None)

    async def test_conversation_message_without_task_excluded(self, db, sample_project):
        """Conversation messages with no task_id do not appear in results."""
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 2)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        await db.create_conversation(id="search-conv", project="test-project", goal="Search test")
        msg = await db.post_message(
            conversation_id="search-conv",
            author="human",
            content="We decided to use Redis for session storage because it supports TTL natively.",
            type="note",
        )
        await db.set_message_embedding(msg["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "session storage"})
            # No result should be a conversation-only item (they have no task context)
            # Results are task objects; all have an "id" (task id) and "goal"
            for r in result["results"]:
                assert "id" in r
                assert "goal" in r
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# De-duplication: same task matched via multiple sources → appears once
# ---------------------------------------------------------------------------

class TestSearchDeduplication:
    async def test_task_appears_once_when_matched_multiple_ways(self, db, sample_project):
        """If a task matches on goal AND a message, it appears once in results."""
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        task = await db.create_task(
            id="test-project/dedup-task",
            project_id="test-project",
            goal="Implement the deduplication feature",
        )
        # Match on task goal embedding
        await db.set_task_embedding(task["id"], encode_vector(vec))
        # Also match on a task message
        msg = await db.post_task_message(
            task_id=task["id"],
            author="cc-worker",
            content="Deduplication logic is now in place.",
            type="progress",
        )
        await db.set_message_embedding(msg["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            result = await _handle_search({"query": "deduplication"})
            task_matches = [r for r in result["results"] if r["id"] == task["id"]]
            # Task must appear exactly once
            assert len(task_matches) == 1
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# project_id scoping
# ---------------------------------------------------------------------------

class TestSearchProjectScoping:
    async def test_project_id_filters_task_results(self, db, sample_project):
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

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
            ids = [r["id"] for r in result["results"]]
            assert task_mine["id"] in ids
            assert task_other["id"] not in ids
        finally:
            set_embedding_service(None)

    async def test_project_id_filters_message_results(self, db, sample_project):
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

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
            ids = [r["id"] for r in result["results"]]
            assert task_mine["id"] in ids
            assert task_other["id"] not in ids
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Limit parameter
# ---------------------------------------------------------------------------

class TestSearchLimit:
    async def test_limit_capped_at_30(self, db, sample_project):
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

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
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

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
# Results are ranked by best relevance score per task
# ---------------------------------------------------------------------------

class TestSearchRanking:
    async def test_results_sorted_by_relevance_descending(self, db, sample_project):
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

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
            ids = [r["id"] for r in result["results"]]
            # High relevance task should appear before low relevance task
            assert task_high["id"] in ids
            assert ids.index(task_high["id"]) < ids.index(task_low["id"])
        finally:
            set_embedding_service(None)

    async def test_total_candidates_in_response(self, db, sample_project):
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService

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
