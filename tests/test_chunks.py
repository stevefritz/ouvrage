"""Tests for message chunking — paragraph-level semantic search.

Covers:
- chunk_message(): markdown splitting on headers, edge cases
- index_message_chunks(): embedding and storage
- search_message_chunks(): retrieval with context window
"""

import pytest
from switchboard.embeddings.chunks import chunk_message, MIN_CHUNK_LENGTH


# ---------------------------------------------------------------------------
# chunk_message() unit tests — pure function, no DB needed
# ---------------------------------------------------------------------------


class TestChunkMessageNoHeaders:

    def test_h4_headers_not_matched(self):
        """Only h1-h3 headers trigger chunking."""
        content = "Intro text\n\n" + "#### Not a chunk header\n\nSome content.\n\n" * 20
        assert len(content) >= 500
        assert chunk_message(content) is None


class TestChunkMessageEdgeCases:


    def test_chunk_indices_contiguous_with_leading_header(self):
        """When content starts with ## header, the split produces a leading empty string.
        chunk_index values must still be contiguous starting at 0."""
        content = (
            "## First\n\n"
            + "Content for first section fills the needed space. " * 5 + "\n\n"
            "## Second\n\n"
            + "Content for second section fills the needed space. " * 5 + "\n\n"
            "## Third\n\n"
            + "Content for third section fills the needed space. " * 5
        )
        assert len(content) >= 500
        result = chunk_message(content)
        assert result is not None
        assert result[0]["chunk_index"] == 0
        assert result[1]["chunk_index"] == 1
        assert result[2]["chunk_index"] == 2

    def test_min_chunk_length_constant(self):
        assert MIN_CHUNK_LENGTH == 500
