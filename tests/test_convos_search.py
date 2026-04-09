"""Tests for v5 component conversations and search_component tool."""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def component(db, sample_project):
    """A component belonging to test-project."""
    return await db.create_component(
        id="search-comp",
        project_id="test-project",
        name="Search Component",
    )


@pytest.fixture
async def linked_conversation(db, sample_project, component):
    """A conversation linked to the component with searchable messages."""
    conv = await db.create_conversation(
        id="design-doc",
        project="test-project",
        goal="Design doc for search component",
    )
    await db.post_message(
        conversation_id="design-doc",
        author="stephen",
        content="We need a fast fuzzy search algorithm here.",
        type="note",
    )
    await db.post_message(
        conversation_id="design-doc",
        author="claude-ai",
        content="I recommend using trigram indexing for performance.",
        type="spec",
    )
    await db.link_conversation("search-comp", "design-doc")
    return conv


@pytest.fixture
async def component_task(db, sample_project, component):
    """A task with component_id=search-comp and some messages."""
    task = await db.create_task(
        id="test-project/comp-task",
        project_id="test-project",
        goal="Implement the search feature",
        component_id="search-comp",
    )
    await db.post_task_message(
        task_id="test-project/comp-task",
        author="cc-worker",
        content="Starting implementation of the search index.",
        type="progress",
    )
    await db.post_task_message(
        task_id="test-project/comp-task",
        author="cc-worker",
        content="Search index complete. Running benchmarks.",
        type="progress",
    )
    return task


# ---------------------------------------------------------------------------
# TestConversationLinking
# ---------------------------------------------------------------------------

class TestConversationLinking:


    async def test_link_bad_component_raises(self, db, sample_project):
        with pytest.raises(ValueError, match="not found"):
            await db.link_conversation("no-such-comp", "some-conv")


# ---------------------------------------------------------------------------
# TestSearchComponent
# ---------------------------------------------------------------------------

class TestSearchComponent:


    async def test_search_merged_results(self, db, sample_project, linked_conversation, component_task):
        # "search" appears in both conv messages and task messages
        result = await db.search_component("search-comp", "search")
        sources = {r["source"] for r in result["results"]}
        assert "conversation" in sources
        assert "task" in sources


    async def test_search_component_not_found(self, db, sample_project):
        with pytest.raises(ValueError, match="not found"):
            await db.search_component("ghost-comp", "anything")


# ---------------------------------------------------------------------------
# TestGraphitiProxy
# ---------------------------------------------------------------------------

class TestGraphitiProxy:
    @pytest.fixture
    async def component_with_graphiti(self, db, sample_project, component):
        """Update test-project to have graphiti connector config."""
        await db.update_project(
            "test-project",
            connectors=json.dumps({
                "graphiti": {
                    "url": "http://graphiti.internal",
                    "group_id": "mcp-switchboard",
                }
            }),
        )
        return component


    async def test_graphiti_error_captured_not_raised(self, db, sample_project, component_with_graphiti):
        """HTTP error from Graphiti is captured in graphiti_error, not raised."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("switchboard.db.search.httpx.AsyncClient", return_value=mock_client):
            result = await db.search_component("search-comp", "anything", include_graphiti=True)

        assert result["graphiti_error"] is not None
        assert "Connection refused" in result["graphiti_error"]
        # Other results still returned normally
        assert "results" in result

    async def test_graphiti_results_merged_with_local(self, db, sample_project, linked_conversation, component_with_graphiti):
        """Graphiti results are appended to local results."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"uuid": "g1", "fact": "Graphiti fact about search", "created_at": "2026-03-01T00:00:00Z"},
        ]
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("switchboard.db.search.httpx.AsyncClient", return_value=mock_client):
            result = await db.search_component("search-comp", "search", include_graphiti=True)

        sources = {r["source"] for r in result["results"]}
        assert "graphiti" in sources
        assert "graphiti" in result["sources"]
