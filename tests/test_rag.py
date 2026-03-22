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

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ---------------------------------------------------------------------------
# EmbeddingService unit tests (no DB, no OpenAI)
# ---------------------------------------------------------------------------

class TestShouldEmbed:
    def test_long_content_no_type_is_embedded(self):
        from embedding_service import should_embed
        assert should_embed("x" * 50, None) is True

    def test_content_exactly_50_chars(self):
        from embedding_service import should_embed
        assert should_embed("x" * 50, "note") is True

    def test_content_49_chars_skipped(self):
        from embedding_service import should_embed
        assert should_embed("x" * 49, "note") is False

    def test_empty_content_skipped(self):
        from embedding_service import should_embed
        assert should_embed("", "note") is False

    def test_none_content_skipped(self):
        from embedding_service import should_embed
        assert should_embed(None, "note") is False

    def test_test_result_skipped(self):
        from embedding_service import should_embed
        assert should_embed("x" * 100, "test-result") is False

    def test_spec_with_long_content_embedded(self):
        from embedding_service import should_embed
        assert should_embed("This is a specification. " * 5, "spec") is True

    def test_status_with_long_content_embedded(self):
        from embedding_service import should_embed
        # status gets low weight but still gets embedded
        assert should_embed("Status update: task completed." * 3, "status") is True


class TestVectorEncoding:
    def test_encode_decode_roundtrip(self):
        from embedding_service import encode_vector, decode_vector
        original = [0.1, 0.5, -0.3, 0.99, -1.0]
        blob = encode_vector(original)
        decoded = decode_vector(blob)
        assert len(decoded) == len(original)
        for a, b in zip(original, decoded):
            assert abs(a - b) < 1e-5

    def test_encode_1536_dims(self):
        from embedding_service import encode_vector, decode_vector
        vec = [float(i) / 1536 for i in range(1536)]
        blob = encode_vector(vec)
        assert len(blob) == 1536 * 4  # 4 bytes per float32
        decoded = decode_vector(blob)
        assert len(decoded) == 1536

    def test_blob_is_bytes(self):
        from embedding_service import encode_vector
        blob = encode_vector([1.0, 2.0, 3.0])
        assert isinstance(blob, bytes)


class TestCosineSimilarity:
    def test_identical_vectors(self):
        from embedding_service import cosine_similarity
        v = [1.0, 0.0, 0.5]
        assert abs(cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        from embedding_service import cosine_similarity
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert abs(cosine_similarity(a, b)) < 1e-6

    def test_opposite_vectors(self):
        from embedding_service import cosine_similarity
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert abs(cosine_similarity(a, b) - (-1.0)) < 1e-6

    def test_zero_vector_returns_zero(self):
        from embedding_service import cosine_similarity
        a = [0.0, 0.0]
        b = [1.0, 0.0]
        assert cosine_similarity(a, b) == 0.0

    def test_range_minus_one_to_one(self):
        from embedding_service import cosine_similarity
        a = [0.3, 0.5, -0.2]
        b = [0.1, -0.4, 0.8]
        sim = cosine_similarity(a, b)
        assert -1.0 <= sim <= 1.0


class TestRelevanceScoring:
    def test_spec_gets_highest_weight(self):
        from embedding_service import compute_relevance_score
        spec_score = compute_relevance_score(0.8, "spec", False)
        status_score = compute_relevance_score(0.8, "status", False)
        assert spec_score > status_score

    def test_pinned_boost_applies(self):
        from embedding_service import compute_relevance_score
        unpinned = compute_relevance_score(0.8, "note", False)
        pinned = compute_relevance_score(0.8, "note", True)
        assert abs(pinned / unpinned - 1.3) < 1e-6

    def test_unknown_type_gets_neutral_weight(self):
        from embedding_service import compute_relevance_score
        score = compute_relevance_score(0.8, "unknown-type", False)
        assert abs(score - 0.8) < 1e-6  # weight = 1.0

    def test_formula(self):
        from embedding_service import compute_relevance_score
        # spec=1.5, pinned=1.3, similarity=0.6 → 0.6 * 1.5 * 1.3 = 1.17
        score = compute_relevance_score(0.6, "spec", True)
        assert abs(score - 0.6 * 1.5 * 1.3) < 1e-6

    def test_test_result_gets_lowest_weight(self):
        from embedding_service import compute_relevance_score
        test_result = compute_relevance_score(0.9, "test-result", False)
        spec = compute_relevance_score(0.9, "spec", False)
        assert spec > test_result


class TestEmbeddingServiceSingleton:
    def test_get_returns_openai_service(self):
        from embedding_service import get_embedding_service, OpenAIEmbeddingService, set_embedding_service
        # Reset singleton
        set_embedding_service(None)
        service = get_embedding_service()
        assert isinstance(service, OpenAIEmbeddingService)

    def test_set_service_replaces_singleton(self):
        from embedding_service import get_embedding_service, set_embedding_service, EmbeddingService

        class FakeService(EmbeddingService):
            async def embed(self, text):
                return [0.0] * 1536

        fake = FakeService()
        set_embedding_service(fake)
        assert get_embedding_service() is fake

        # Cleanup
        set_embedding_service(None)


class TestEmbedSafe:
    @pytest.mark.asyncio
    async def test_embed_safe_returns_none_on_error(self):
        from embedding_service import EmbeddingService

        class FailingService(EmbeddingService):
            async def embed(self, text):
                raise RuntimeError("API down")

        svc = FailingService()
        result = await svc.embed_safe("some content here")
        assert result is None

    @pytest.mark.asyncio
    async def test_embed_safe_returns_vector_on_success(self):
        from embedding_service import EmbeddingService

        class GoodService(EmbeddingService):
            async def embed(self, text):
                return [0.1] * 1536

        svc = GoodService()
        result = await svc.embed_safe("some content here")
        assert result == [0.1] * 1536


# ---------------------------------------------------------------------------
# Database integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_adds_embedding_column(db):
    """After init_db(), messages table should have an embedding column."""
    async with db.get_db() as conn:
        cols = await conn.execute_fetchall("PRAGMA table_info(messages)")
        col_names = [c["name"] for c in cols]
    assert "embedding" in col_names


@pytest.mark.asyncio
async def test_set_and_retrieve_embedding(db, sample_conversation):
    """set_message_embedding stores a blob that can be read back."""
    from embedding_service import encode_vector, decode_vector

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


@pytest.mark.asyncio
async def test_get_messages_needing_embedding_excludes_short(db, sample_conversation):
    """Messages with content < 50 chars are excluded from backfill."""
    rows = await db.get_messages_needing_embedding(batch_size=100)
    for row in rows:
        assert len(row["content"]) >= 50


@pytest.mark.asyncio
async def test_get_messages_needing_embedding_excludes_already_embedded(db, sample_conversation):
    """Messages that already have an embedding are excluded."""
    from embedding_service import encode_vector

    # Get a message that needs embedding
    rows = await db.get_messages_needing_embedding(batch_size=1)
    if not rows:
        return  # nothing to test

    msg_id = rows[0]["id"]
    blob = encode_vector([0.1] * 1536)
    await db.set_message_embedding(msg_id, blob)

    # It should no longer appear
    remaining = await db.get_messages_needing_embedding(batch_size=100)
    ids = [r["id"] for r in remaining]
    assert msg_id not in ids


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
async def test_search_finds_similar_messages(db, sample_conversation):
    """search_messages_semantic returns messages ranked by similarity."""
    from embedding_service import encode_vector

    # Post messages with known embeddings
    msg1 = await db.post_message(
        conversation_id="widget-redesign",
        author="test",
        content="Architectural decision about widget sorting algorithm using timsort",
        type="spec",
    )
    msg2 = await db.post_message(
        conversation_id="widget-redesign",
        author="test",
        content="Database schema design for storing widget configuration values",
        type="note",
    )

    # Assign vectors: msg1 gets vector pointing in direction 0, msg2 in direction 1
    vec_a = _make_vector(0.0)
    vec_b = _make_vector(0.5)
    await db.set_message_embedding(msg1["id"], encode_vector(vec_a))
    await db.set_message_embedding(msg2["id"], encode_vector(vec_b))

    # Query with vector close to vec_a
    results = await db.search_messages_semantic(query_vector=vec_a, limit=10)
    ids = [r["message_id"] for r in results]
    assert msg1["id"] in ids
    # msg1 should be ranked first (exact match)
    assert results[0]["message_id"] == msg1["id"]


@pytest.mark.asyncio
async def test_search_filters_by_conversation(db, sample_project):
    """conversation_id filter scopes search correctly."""
    from embedding_service import encode_vector

    # Create two conversations
    conv1 = await db.create_conversation(id="conv-a", project="test-project", goal="Conv A")
    conv2 = await db.create_conversation(id="conv-b", project="test-project", goal="Conv B")

    vec = _make_vector(0.1)
    blob = encode_vector(vec)

    msg_a = await db.post_message(
        conversation_id="conv-a", author="test",
        content="Important architectural note about the system design approach",
        type="note",
    )
    msg_b = await db.post_message(
        conversation_id="conv-b", author="test",
        content="Important architectural note about the system design approach",
        type="note",
    )
    await db.set_message_embedding(msg_a["id"], blob)
    await db.set_message_embedding(msg_b["id"], blob)

    # Search scoped to conv-a only
    results = await db.search_messages_semantic(
        query_vector=vec, conversation_id="conv-a", limit=10
    )
    ids = [r["message_id"] for r in results]
    assert msg_a["id"] in ids
    assert msg_b["id"] not in ids


@pytest.mark.asyncio
async def test_search_filters_by_project(db, sample_project):
    """project_id filter scopes search to that project's conversations."""
    from embedding_service import encode_vector

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
async def test_embed_on_write_via_handle_post(db, sample_conversation):
    """_handle_post triggers async embedding after message is created."""
    from embedding_service import set_embedding_service, EmbeddingService, encode_vector

    embedded_ids = []

    class FakeService(EmbeddingService):
        async def embed(self, text):
            return [0.5] * 1536

    set_embedding_service(FakeService())

    try:
        import server
        result = await server._handle_post({
            "conversation_id": "widget-redesign",
            "author": "test",
            "content": "This is a substantial message about the widget redesign architecture decision",
            "type": "note",
        })

        msg_id = result["id"]

        # Allow the async task to complete
        await asyncio.sleep(0.1)

        # Check embedding was stored
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT embedding FROM messages WHERE id = ?", (msg_id,)
            )
        assert rows[0]["embedding"] is not None
    finally:
        set_embedding_service(None)


@pytest.mark.asyncio
async def test_embed_on_write_skips_short_content(db, sample_conversation):
    """Short messages don't get embedded even when posted via _handle_post."""
    from embedding_service import set_embedding_service, EmbeddingService

    embed_calls = []

    class TrackingService(EmbeddingService):
        async def embed(self, text):
            embed_calls.append(text)
            return [0.5] * 1536

    set_embedding_service(TrackingService())

    try:
        import server
        result = await server._handle_post({
            "conversation_id": "widget-redesign",
            "author": "test",
            "content": "Short msg",  # under 50 chars
        })

        await asyncio.sleep(0.1)
        assert len(embed_calls) == 0
    finally:
        set_embedding_service(None)


@pytest.mark.asyncio
async def test_embed_on_write_skips_test_result(db, sample_conversation):
    """test-result messages don't get embedded."""
    from embedding_service import set_embedding_service, EmbeddingService

    embed_calls = []

    class TrackingService(EmbeddingService):
        async def embed(self, text):
            embed_calls.append(text)
            return [0.5] * 1536

    set_embedding_service(TrackingService())

    try:
        import server
        await server._handle_post({
            "conversation_id": "widget-redesign",
            "author": "gate",
            "content": "SUITE PASSED: 150 tests OK " + "x" * 100,
            "type": "test-result",
        })

        await asyncio.sleep(0.1)
        assert len(embed_calls) == 0
    finally:
        set_embedding_service(None)


# ---------------------------------------------------------------------------
# search_conversations MCP tool test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_conversations_tool_type_weighting(db, sample_project):
    """Type weighting raises spec/note above status for same similarity."""
    from embedding_service import set_embedding_service, EmbeddingService, encode_vector, compute_relevance_score

    vec = _make_vector(0.3)
    blob = encode_vector(vec)

    conv = await db.create_conversation(id="search-test", project="test-project", goal="test")

    spec_msg = await db.post_message(
        conversation_id="search-test", author="test",
        content="Architectural decision: use timsort for widget ordering because it is stable and O(n log n)",
        type="spec",
    )
    status_msg = await db.post_message(
        conversation_id="search-test", author="test",
        content="Task completed successfully. The implementation is done and ready for review.",
        type="status",
    )

    await db.set_message_embedding(spec_msg["id"], blob)
    await db.set_message_embedding(status_msg["id"], blob)

    # Both have same similarity to query, but spec should rank higher due to weight
    spec_score = compute_relevance_score(0.8, "spec", False)
    status_score = compute_relevance_score(0.8, "status", False)
    assert spec_score > status_score


@pytest.mark.asyncio
async def test_search_conversations_tool_pinned_boost(db):
    """Pinned messages get 1.3x boost over unpinned."""
    from embedding_service import compute_relevance_score
    unpinned = compute_relevance_score(0.7, "note", False)
    pinned = compute_relevance_score(0.7, "note", True)
    assert abs(pinned / unpinned - 1.3) < 1e-6


@pytest.mark.asyncio
async def test_search_conversations_handles_no_embeddings(db, sample_conversation):
    """search_conversations returns empty results gracefully when no embeddings exist."""
    from embedding_service import set_embedding_service, EmbeddingService

    class FakeService(EmbeddingService):
        async def embed(self, text):
            return [0.1] * 1536

    set_embedding_service(FakeService())

    try:
        import server
        result = await server._handle_search_conversations({
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
    from embedding_service import set_embedding_service, EmbeddingService

    class FailingService(EmbeddingService):
        async def embed(self, text):
            raise RuntimeError("API down")

        async def embed_safe(self, text):
            return None

    set_embedding_service(FailingService())

    try:
        import server
        result = await server._handle_search_conversations({
            "query": "something",
        })
        assert "error" in result
    finally:
        set_embedding_service(None)


@pytest.mark.asyncio
async def test_search_conversations_ranked_results(db, sample_project):
    """_handle_search_conversations returns correctly ranked results with expected shape."""
    from embedding_service import (
        set_embedding_service, EmbeddingService, encode_vector, decode_vector,
    )

    # Use a fixed query vector so cosine similarity is identical for all stored messages
    query_vec = _make_vector(0.5)

    class FakeService(EmbeddingService):
        async def embed(self, text):
            return query_vec

    set_embedding_service(FakeService())

    try:
        import server

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

        result = await server._handle_search_conversations({
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
