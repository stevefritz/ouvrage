"""Tests for the unified `search` MCP tool.

Covers:
- Returns error when embedding fails (no OPENAI_API_KEY)
- Searches task goals, conversation messages, and task messages
- Chunks supersede parent messages when both match (deduplication)
- Results are typed correctly (task, conversation_message, task_message, chunk)
- Optional project_id scopes results
- Limit parameter respected (max 30)
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
# Task results
# ---------------------------------------------------------------------------

class TestSearchTaskResults:
    async def test_task_results_returned(self, db, sample_project):
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
            task_results = [r for r in result["results"] if r["type"] == "task"]
            assert len(task_results) >= 1
            first = task_results[0]
            assert first["task_id"] == task["id"]
            assert first["title"] == "Implement the authentication module"
            assert first["snippet"] == "Implement the authentication module"
            assert 0.0 <= first["relevance_score"] <= 1.0
        finally:
            set_embedding_service(None)

    async def test_task_result_fields(self, db, sample_project):
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
            task_results = [r for r in result["results"] if r["type"] == "task"]
            assert len(task_results) >= 1
            r = task_results[0]
            assert r["type"] == "task"
            assert "task_id" in r
            assert "title" in r
            assert "snippet" in r
            assert "relevance_score" in r
            assert "created_at" in r
            assert "conversation_id" in r
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Message results (conversation_message and task_message)
# ---------------------------------------------------------------------------

class TestSearchMessageResults:
    async def test_conversation_message_type(self, db, sample_project):
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
            conv_results = [r for r in result["results"] if r["type"] == "conversation_message"]
            assert len(conv_results) >= 1
            r = conv_results[0]
            assert r["conversation_id"] == "search-conv"
            assert r["task_id"] is None
        finally:
            set_embedding_service(None)

    async def test_task_message_type(self, db, sample_project):
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
            task_msg_results = [r for r in result["results"] if r["type"] == "task_message"]
            assert len(task_msg_results) >= 1
            r = task_msg_results[0]
            assert r["task_id"] == task["id"]
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Chunk deduplication
# ---------------------------------------------------------------------------

class TestSearchChunkDeduplication:
    async def test_chunk_supersedes_parent_message(self, db, sample_project):
        """If a message AND a chunk from that message match, only the chunk appears."""
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector
        from switchboard.db.search import index_message_chunks

        vec = _unit_vec(4, 0)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        await db.create_conversation(id="dedup-conv", project="test-project", goal="Dedup test")
        long_content = "# Section One\n\n" + "A" * 200 + "\n\n# Section Two\n\n" + "B" * 200

        msg = await db.post_message(
            conversation_id="dedup-conv",
            author="human",
            content=long_content,
            type="spec",
        )
        # Set embedding on the message itself
        await db.set_message_embedding(msg["id"], encode_vector(vec))

        set_embedding_service(MockService())
        try:
            # Index chunks so search_message_chunks can find them
            await index_message_chunks(msg["id"], long_content)

            result = await _handle_search({"query": "section content"})
            results_by_msg_id = {}
            for r in result["results"]:
                if r["type"] == "chunk":
                    results_by_msg_id["chunk"] = r
                elif r["type"] in ("conversation_message", "task_message"):
                    results_by_msg_id["message"] = r

            # The message should NOT appear if chunk covers it
            if "chunk" in results_by_msg_id:
                # If a chunk was returned, the raw message should not be in results
                message_results = [r for r in result["results"]
                                   if r["type"] in ("conversation_message", "task_message")]
                # No message result should come from the same parent message as the chunk
                # (deduplication: chunk supersedes parent)
                assert "message" not in results_by_msg_id, (
                    "Parent message should be dropped when its chunk is present"
                )
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
            task_ids = [r["task_id"] for r in result["results"] if r["type"] == "task"]
            assert task_mine["id"] in task_ids
            assert task_other["id"] not in task_ids
        finally:
            set_embedding_service(None)

    async def test_project_id_filters_message_results(self, db, sample_project):
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        vec = _unit_vec(4, 1)

        class MockService(EmbeddingService):
            async def embed(self, text):
                return vec

        await db.create_project(id="other-proj2", repo="https://github.com/x/z.git", working_dir="/work/z", default_branch="main")
        await db.create_conversation(id="my-conv", project="test-project", goal="My conv")
        await db.create_conversation(id="other-conv", project="other-proj2", goal="Other conv")

        msg_mine = await db.post_message(
            conversation_id="my-conv",
            author="human",
            content="This message is in test-project and has plenty of content to embed.",
            type="note",
        )
        msg_other = await db.post_message(
            conversation_id="other-conv",
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
            conv_ids = [r.get("conversation_id") for r in result["results"]
                        if r["type"] == "conversation_message"]
            assert "my-conv" in conv_ids
            assert "other-conv" not in conv_ids
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
# Results are ranked by relevance_score
# ---------------------------------------------------------------------------

class TestSearchRanking:
    async def test_results_sorted_by_relevance_descending(self, db, sample_project):
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

        query_vec = _unit_vec(4, 0)
        other_vec = _unit_vec(4, 1)

        calls = [0]

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
            scores = [r["relevance_score"] for r in result["results"]]
            assert scores == sorted(scores, reverse=True)
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
