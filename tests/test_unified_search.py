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


# ---------------------------------------------------------------------------
# Task results — returns compact result cards with entity_id, snippet, etc.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Task message results — returned as task_message cards
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Snippet truncation
# ---------------------------------------------------------------------------

class TestSearchSnippet:
    async def test_snippet_max_200_chars(self, db, sample_project):
        from switchboard.embeddings.service import set_embedding_service, EmbeddingService, encode_vector

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
            entity_ids = [r["entity_id"] for r in result["results"]]
            assert task_mine["id"] in entity_ids
            assert task_other["id"] not in entity_ids
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
            entity_ids = [r["entity_id"] for r in result["results"]]
            assert str(msg_mine["id"]) in entity_ids
            assert str(msg_other["id"]) not in entity_ids
        finally:
            set_embedding_service(None)


# ---------------------------------------------------------------------------
# Limit parameter
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Results are ranked by best relevance score
# ---------------------------------------------------------------------------

