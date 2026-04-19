"""Tests for message chunking — paragraph-level semantic search.

Covers:
- chunk_message(): markdown splitting on headers, edge cases
- index_message_chunks(): embedding and storage
- search_message_chunks(): retrieval with context window
"""

import pytest
from ouvrage.embeddings.chunks import chunk_message, MIN_CHUNK_LENGTH


# ---------------------------------------------------------------------------
# chunk_message() unit tests — pure function, no DB needed
# ---------------------------------------------------------------------------

class TestChunkMessageShortContent:
    def test_under_500_chars_returns_none(self):
        assert chunk_message("x" * 499) is None

    def test_exactly_500_chars_no_headers_returns_none(self):
        assert chunk_message("x" * 500) is None

    def test_empty_string_returns_none(self):
        assert chunk_message("") is None


class TestChunkMessageNoHeaders:
    def test_long_content_no_headers_returns_none(self):
        content = "This is a long paragraph.\n\n" * 50
        assert len(content) >= 500
        assert chunk_message(content) is None

    def test_h4_headers_not_matched(self):
        """Only h1-h3 headers trigger chunking."""
        content = "Intro text\n\n" + "#### Not a chunk header\n\nSome content.\n\n" * 20
        assert len(content) >= 500
        assert chunk_message(content) is None


class TestChunkMessageSingleSection:
    def test_single_header_returns_none(self):
        content = "## Only One Section\n\n" + "Content goes here and fills up space. " * 15
        assert len(content) >= 500
        assert chunk_message(content) is None


class TestChunkMessageMultipleSections:
    def test_two_h2_headers(self):
        content = (
            "## First Section\n\n"
            + "First section content is here and it keeps going. " * 6 + "\n\n"
            "## Second Section\n\n"
            + "Second section content is here and it keeps going. " * 6
        )
        assert len(content) >= 500
        result = chunk_message(content)
        assert result is not None
        assert len(result) == 2
        assert result[0]["heading"] == "First Section"
        assert result[1]["heading"] == "Second Section"
        assert "First section content" in result[0]["content"]
        assert "Second section content" in result[1]["content"]

    def test_three_sections_with_preamble(self):
        content = (
            "Some preamble text before any header that gives context.\n\n"
            "## Section A\n\n"
            + "Content for section A goes on and on. " * 5 + "\n\n"
            "## Section B\n\n"
            + "Content for section B goes on and on. " * 5 + "\n\n"
            "## Section C\n\n"
            + "Content for section C goes on and on. " * 5
        )
        assert len(content) >= 500
        result = chunk_message(content)
        assert result is not None
        # Preamble + 3 sections = 4 chunks
        assert len(result) >= 3
        # Preamble has no heading
        preamble = [c for c in result if c["heading"] is None]
        assert len(preamble) == 1

    def test_nested_headers_h2_and_h3(self):
        content = (
            "## Top Level\n\n"
            + "Some top level content that goes on for a while to fill space. " * 4 + "\n\n"
            "### Subsection\n\n"
            + "Subsection content that also goes on for a while to fill space. " * 4 + "\n\n"
            "## Another Top Level\n\n"
            + "More content here that fills up the remaining space nicely. " * 4
        )
        assert len(content) >= 500
        result = chunk_message(content)
        assert result is not None
        assert len(result) == 3
        headings = [c["heading"] for c in result]
        assert "Top Level" in headings
        assert "Subsection" in headings
        assert "Another Top Level" in headings

    def test_h1_headers_work(self):
        content = (
            "# First\n\n"
            + "Content for first section that fills up the necessary space. " * 5 + "\n\n"
            "# Second\n\n"
            + "Content for second section that fills up the necessary space. " * 5
        )
        assert len(content) >= 500
        result = chunk_message(content)
        assert result is not None
        assert len(result) == 2
        assert result[0]["heading"] == "First"
        assert result[1]["heading"] == "Second"

    def test_chunk_index_is_set(self):
        content = (
            "## A\n\n"
            + "Content for section A fills the space. " * 5 + "\n\n"
            "## B\n\n"
            + "Content for section B fills the space. " * 5 + "\n\n"
            "## C\n\n"
            + "Content for section C fills the space. " * 5
        )
        assert len(content) >= 500
        result = chunk_message(content)
        assert result is not None
        # Indices must be contiguous starting at 0
        for i, chunk in enumerate(result):
            assert chunk["chunk_index"] == i

    def test_empty_sections_stripped(self):
        """Empty sections from splitting should be filtered out."""
        content = (
            "## First\n\n"
            + "Real content here that fills space adequately for testing. " * 5 + "\n\n"
            "## Second\n\n"
            + "More real content here that fills space adequately for testing. " * 5
        )
        assert len(content) >= 500
        result = chunk_message(content)
        assert result is not None
        for chunk in result:
            assert chunk["content"].strip() != ""


class TestChunkMessageEdgeCases:
    def test_header_at_very_start(self):
        content = (
            "## Start\n\n"
            + "Content for start section that fills the needed space. " * 5 + "\n\n"
            "## End\n\n"
            + "More content for end section that fills the needed space. " * 5
        )
        assert len(content) >= 500
        result = chunk_message(content)
        assert result is not None
        assert result[0]["heading"] == "Start"

    def test_header_with_special_chars(self):
        content = (
            "## Section: The `code` & stuff\n\n"
            + "Content for first section that fills space for the test. " * 5 + "\n\n"
            "## Another — section\n\n"
            + "More content for second section that fills space for test. " * 5
        )
        assert len(content) >= 500
        result = chunk_message(content)
        assert result is not None
        assert "The `code` & stuff" in result[0]["heading"]

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
