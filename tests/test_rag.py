"""Tests for the conversation RAG feature.

Covers:
- Embedding service: vector encoding/decoding, cosine similarity, should_embed logic
- Database migrations: embedding column added
- Embed-on-write: creates embedding for new messages
- Skip conditions: messages < 50 chars, test-result type
- search_conversations: type weighting, pinned boost, project scoping
- Backfill: processes messages needing embedding
"""

import asyncio
import math
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from switchboard.server.handlers.conversations import _handle_post, _handle_search_conversations


# ---------------------------------------------------------------------------
# EmbeddingService unit tests (no DB, no OpenAI)
# ---------------------------------------------------------------------------

class TestShouldEmbed:


    def test_content_49_chars_skipped(self):
        from switchboard.embeddings.service import should_embed
        assert should_embed("x" * 49, "note") is False


    def test_test_result_skipped(self):
        from switchboard.embeddings.service import should_embed
        assert should_embed("x" * 100, "test-result") is False


class TestCosineSimilarity:
    def test_identical_vectors(self):
        from switchboard.embeddings.service import cosine_similarity
        v = [1.0, 0.0, 0.5]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-6


    def test_zero_vector_returns_zero(self):
        from switchboard.embeddings.service import cosine_similarity
        a = [0.0, 0.0]
        b = [1.0, 0.0]
        assert cosine_similarity(a, b) == 0.0


class TestEmbeddingServiceSingleton:
    def test_get_returns_openai_service(self):
        from switchboard.embeddings.service import get_embedding_service, OpenAIEmbeddingService, set_embedding_service
        # Reset singleton
        set_embedding_service(None)
        service = get_embedding_service()
        assert isinstance(service, OpenAIEmbeddingService)


# ---------------------------------------------------------------------------
# Database integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_retrieve_embedding(db, sample_conversation):
    """set_message_embedding stores a blob that can be read back."""
    from switchboard.embeddings.service import encode_vector, decode_vector

    # Post a message
    msg = await db.post_message(
        conversation_id="widget-redesign",
        author="test",
        content="A" * 100,
    )
    msg_id = msg["id"]

    # Store a fake embedding
    vector = [float(i) / 1536 for i in range(1536)]
    blob = encode_vector(vector)
    await db.set_message_embedding(msg_id, blob)

    # Read it back
    async with db.get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT embedding FROM messages WHERE id = ?", (msg_id,)
        )
    assert rows[0]["embedding"] is not None
    decoded = decode_vector(rows[0]["embedding"])
    assert len(decoded) == 1536
    for a, b in zip(vector[:10], decoded[:10]):
        assert abs(a - b) < 1e-5


@pytest.mark.asyncio
async def test_count_messages_needing_embedding(db, sample_conversation):
    """count_messages_needing_embedding returns correct count."""
    count = await db.count_messages_needing_embedding()
    # sample_conversation has 3 messages: note (long enough), spec (long enough), status (too short)
    # "Implemented. PR ready for review." = 35 chars — under 50 → skipped
    # The other two are long enough
    assert count >= 1


@pytest.mark.asyncio
async def test_get_messages_needing_embedding_excludes_test_result(db, sample_conversation):
    """Messages with type=test-result are excluded from backfill."""
    # Post a test-result message with long content
    await db.post_message(
        conversation_id="widget-redesign",
        author="gate",
        content="PASSED: " + "test output " * 20,
        type="test-result",
    )
    rows = await db.get_messages_needing_embedding(batch_size=100)
    for row in rows:
        assert row["type"] != "test-result"


# ---------------------------------------------------------------------------
# search_messages_semantic tests
# ---------------------------------------------------------------------------

def _make_vector(seed: float) -> list[float]:
    """Make a deterministic unit vector from a seed value."""
    # Simple: one dimension is 1.0, rest 0.0 — for easy cosine math
    v = [0.0] * 1536
    idx = int(seed * 1535) % 1536
    v[idx] = 1.0
    return v


@pytest.mark.asyncio
async def test_search_filters_by_project(db, sample_project):
    """project_id filter scopes search to that project's conversations."""
    from switchboard.embeddings.service import encode_vector

    conv = await db.create_conversation(
        id="proj-conv", project="test-project", goal="Project conversation"
    )

    # Create a second project + conversation
    await db.create_project(
        id="other-project", repo="git@github.com:x/y.git",
        working_dir="/work/y", default_branch="main",
    )
    conv_other = await db.create_conversation(
        id="other-conv", project="other-project", goal="Other conversation"
    )

    vec = _make_vector(0.2)
    blob = encode_vector(vec)

    msg_mine = await db.post_message(
        conversation_id="proj-conv", author="test",
        content="This is a spec for the test project architecture design",
        type="spec",
    )
    msg_other = await db.post_message(
        conversation_id="other-conv", author="test",
        content="This is a spec for the test project architecture design",
        type="spec",
    )
    await db.set_message_embedding(msg_mine["id"], blob)
    await db.set_message_embedding(msg_other["id"], blob)

    results = await db.search_messages_semantic(
        query_vector=vec, project_id="test-project", limit=10
    )
    ids = [r["message_id"] for r in results]
    assert msg_mine["id"] in ids
    assert msg_other["id"] not in ids


# ---------------------------------------------------------------------------
# Embed-on-write integration test (with mocked OpenAI)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embed_on_write_skips_short_content(db, sample_conversation):
    """Short messages don't get embedded even when posted via _handle_post."""
    from switchboard.embeddings.service import set_embedding_service, EmbeddingService

    embed_calls = []

    class TrackingService(EmbeddingService):
        async def embed(self, text):
            embed_calls.append(text)
            return [0.5] * 1536

    set_embedding_service(TrackingService())

    try:
        result = await _handle_post({
            "conversation_id": "widget-redesign",
            "author": "test",
            "content": "Short msg",  # under 50 chars
        })

        await asyncio.sleep(0.1)
        assert len(embed_calls) == 0
    finally:
        set_embedding_service(None)


# ---------------------------------------------------------------------------
# search_conversations MCP tool test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_conversations_handles_no_embeddings(db, sample_conversation):
    """search_conversations returns empty results gracefully when no embeddings exist."""
    from switchboard.embeddings.service import set_embedding_service, EmbeddingService

    class FakeService(EmbeddingService):
        async def embed(self, text):
            return [0.1] * 1536

    set_embedding_service(FakeService())

    try:
        result = await _handle_search_conversations({
            "query": "widget sorting algorithm",
            "conversation_id": "widget-redesign",
        })

        assert "results" in result
        assert isinstance(result["results"], list)
        # No embeddings stored yet → empty
        assert len(result["results"]) == 0
    finally:
        set_embedding_service(None)


@pytest.mark.asyncio
async def test_search_conversations_api_error_returns_error(db):
    """search_conversations returns error dict when embedding fails."""
    from switchboard.embeddings.service import set_embedding_service, EmbeddingService

    class FailingService(EmbeddingService):
        async def embed(self, text):
            raise RuntimeError("API down")

        async def embed_safe(self, text):
            return None

    set_embedding_service(FailingService())

    try:
        result = await _handle_search_conversations({
            "query": "something",
        })
        assert "error" in result
    finally:
        set_embedding_service(None)


@pytest.mark.asyncio
async def test_search_conversations_ranked_results(db, sample_project):
    """_handle_search_conversations returns correctly ranked results with expected shape."""
    from switchboard.embeddings.service import (
        set_embedding_service, EmbeddingService, encode_vector, decode_vector,
    )

    # Use a fixed query vector so cosine similarity is identical for all stored messages
    query_vec = _make_vector(0.5)

    class FakeService(EmbeddingService):
        async def embed(self, text):
            return query_vec

    set_embedding_service(FakeService())

    try:
        conv = await db.create_conversation(
            id="ranked-test", project="test-project", goal="Ranking integration test"
        )

        # Store three messages with equal cosine similarity but different types
        spec_msg = await db.post_message(
            conversation_id="ranked-test", author="tester",
            content="Architecture decision: store embeddings as packed float32 BLOBs in SQLite",
            type="spec", title="Embedding storage decision",
        )
        note_msg = await db.post_message(
            conversation_id="ranked-test", author="tester",
            content="Note: cosine similarity works fine at 5K messages without ANN indexing",
            type="note", title=None,
        )
        status_msg = await db.post_message(
            conversation_id="ranked-test", author="tester",
            content="Status update: embedding migration complete and backfill script is ready",
            type="status", title=None,
        )

        blob = encode_vector(query_vec)
        await db.set_message_embedding(spec_msg["id"], blob)
        await db.set_message_embedding(note_msg["id"], blob)
        await db.set_message_embedding(status_msg["id"], blob)

        result = await _handle_search_conversations({
            "query": "embedding storage approach",
            "conversation_id": "ranked-test",
            "max_results": 5,
        })

        assert "results" in result
        results = result["results"]
        assert len(results) == 3

        # Verify ranking: spec (1.5x) > note (1.2x) > status (0.5x)
        types = [r["type"] for r in results]
        assert types.index("spec") < types.index("note")
        assert types.index("note") < types.index("status")

        # Verify response shape matches spec
        for r in results:
            assert "message_id" in r
            assert "conversation_id" in r
            assert "author" in r
            assert "type" in r
            assert "title" in r
            assert "content" in r
            assert "relevance_score" in r
            assert "created_at" in r
            assert len(r["content"]) <= 500
            assert r["author"] == "tester"
            assert r["conversation_id"] == "ranked-test"
    finally:
        set_embedding_service(None)
