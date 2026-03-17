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
    async def test_link_conversation(self, db, sample_project, component):
        result = await db.link_conversation("search-comp", "some-conv")
        # link_conversation doesn't check if the conversation exists (no FK)
        assert result["linked"] is True
        assert result["component_id"] == "search-comp"
        assert result["conversation_id"] == "some-conv"

    async def test_link_conversation_idempotent(self, db, sample_project, component):
        await db.link_conversation("search-comp", "some-conv")
        # Second call should not raise (INSERT OR IGNORE)
        result = await db.link_conversation("search-comp", "some-conv")
        assert result["linked"] is True

    async def test_unlink_conversation(self, db, sample_project, component):
        await db.link_conversation("search-comp", "some-conv")
        result = await db.unlink_conversation("search-comp", "some-conv")
        assert result["unlinked"] is True

        convs = await db.get_component_conversations("search-comp")
        assert "some-conv" not in convs

    async def test_unlink_nonexistent_is_silent(self, db, sample_project, component):
        result = await db.unlink_conversation("search-comp", "ghost-conv")
        assert result["unlinked"] is True  # DELETE with no rows is not an error

    async def test_link_bad_component_raises(self, db, sample_project):
        with pytest.raises(ValueError, match="not found"):
            await db.link_conversation("no-such-comp", "some-conv")

    async def test_get_component_includes_conversations(self, db, sample_project, component):
        await db.link_conversation("search-comp", "conv-a")
        await db.link_conversation("search-comp", "conv-b")
        comp = await db.get_component("search-comp")
        assert "conv-a" in comp["conversations"]
        assert "conv-b" in comp["conversations"]
        assert len(comp["conversations"]) == 2

    async def test_get_component_conversations_helper(self, db, sample_project, component):
        await db.link_conversation("search-comp", "c1")
        await db.link_conversation("search-comp", "c2")
        convs = await db.get_component_conversations("search-comp")
        assert set(convs) == {"c1", "c2"}

    async def test_multiple_components_isolated(self, db, sample_project, component):
        """Conversations linked to one component don't bleed into another."""
        await db.create_component(id="other-comp", project_id="test-project", name="Other")
        await db.link_conversation("search-comp", "shared-conv")
        await db.link_conversation("other-comp", "other-conv")

        search_convs = await db.get_component_conversations("search-comp")
        other_convs = await db.get_component_conversations("other-comp")

        assert "shared-conv" in search_convs
        assert "other-conv" not in search_convs
        assert "shared-conv" not in other_convs


# ---------------------------------------------------------------------------
# TestSearchComponent
# ---------------------------------------------------------------------------

class TestSearchComponent:
    async def test_search_conversation_messages(self, db, sample_project, linked_conversation):
        result = await db.search_component("search-comp", "fuzzy")
        assert result["total"] >= 1
        hits = result["results"]
        matching = [r for r in hits if r["source"] == "conversation"]
        assert len(matching) >= 1
        assert any("fuzzy" in r["snippet"].lower() for r in matching)

    async def test_search_task_messages(self, db, sample_project, component_task):
        result = await db.search_component("search-comp", "index")
        assert result["total"] >= 1
        hits = result["results"]
        task_hits = [r for r in hits if r["source"] == "task"]
        assert len(task_hits) >= 1
        assert any("index" in r["snippet"].lower() for r in task_hits)

    async def test_search_merged_results(self, db, sample_project, linked_conversation, component_task):
        # "search" appears in both conv messages and task messages
        result = await db.search_component("search-comp", "search")
        sources = {r["source"] for r in result["results"]}
        assert "conversation" in sources
        assert "task" in sources

    async def test_search_no_match(self, db, sample_project, linked_conversation, component_task):
        result = await db.search_component("search-comp", "zzz_no_match_xyz")
        assert result["total"] == 0
        assert result["results"] == []

    async def test_search_results_sorted_by_recency(self, db, sample_project, linked_conversation, component_task):
        result = await db.search_component("search-comp", "search")
        timestamps = [r["created_at"] for r in result["results"]]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_search_component_not_found(self, db, sample_project):
        with pytest.raises(ValueError, match="not found"):
            await db.search_component("ghost-comp", "anything")

    async def test_search_empty_component(self, db, sample_project, component):
        """Component with no conversations or tasks returns empty results."""
        result = await db.search_component("search-comp", "anything")
        assert result["total"] == 0
        assert result["results"] == []

    async def test_search_unlinked_conversation_excluded(self, db, sample_project, component):
        """Messages from conversations not linked to this component are excluded."""
        unlinked = await db.create_conversation(
            id="unlinked-conv", project="test-project", goal="Not linked"
        )
        await db.post_message(
            conversation_id="unlinked-conv",
            author="stephen",
            content="This message has the word algorithm in it.",
            type="note",
        )
        # Don't link it — should not appear in search
        result = await db.search_component("search-comp", "algorithm")
        assert result["total"] == 0

    async def test_search_result_schema(self, db, sample_project, linked_conversation):
        result = await db.search_component("search-comp", "trigram")
        assert len(result["results"]) >= 1
        r = result["results"][0]
        assert "source" in r
        assert "snippet" in r
        assert "author" in r
        assert "created_at" in r
        assert "content" not in r  # content replaced by snippet

    async def test_search_sources_field(self, db, sample_project, linked_conversation, component_task):
        result = await db.search_component("search-comp", "search")
        assert isinstance(result["sources"], list)
        for s in result["sources"]:
            assert s in ("conversation", "task", "graphiti")


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

    async def test_graphiti_skipped_by_default(self, db, sample_project, component_with_graphiti):
        """include_graphiti=False (default) should not call Graphiti."""
        with patch("database.httpx.AsyncClient") as mock_client_cls:
            result = await db.search_component("search-comp", "anything")
        mock_client_cls.assert_not_called()
        assert result["graphiti_error"] is None

    async def test_graphiti_called_when_requested(self, db, sample_project, component_with_graphiti):
        """include_graphiti=True should POST to the configured URL."""
        mock_response = MagicMock()
        mock_response.json.return_value = [
            {
                "uuid": "abc123",
                "fact": "Search uses trigram indexing",
                "created_at": "2026-03-01T00:00:00Z",
            }
        ]
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("database.httpx.AsyncClient", return_value=mock_client):
            result = await db.search_component("search-comp", "trigram", include_graphiti=True)

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert "graphiti.internal" in call_args[0][0]
        assert call_args[1]["json"]["query"] == "trigram"
        assert call_args[1]["json"]["group_id"] == "mcp-switchboard"

        graphiti_hits = [r for r in result["results"] if r["source"] == "graphiti"]
        assert len(graphiti_hits) == 1
        assert graphiti_hits[0]["snippet"] == "Search uses trigram indexing"
        assert result["graphiti_error"] is None

    async def test_graphiti_skipped_when_not_configured(self, db, sample_project, component):
        """No connectors config → Graphiti silently skipped, no error."""
        with patch("database.httpx.AsyncClient") as mock_client_cls:
            result = await db.search_component("search-comp", "anything", include_graphiti=True)
        mock_client_cls.assert_not_called()
        assert result["graphiti_error"] is None

    async def test_graphiti_skipped_when_partial_config(self, db, sample_project, component):
        """Graphiti config missing group_id → silently skipped."""
        await db.update_project(
            "test-project",
            connectors=json.dumps({"graphiti": {"url": "http://graphiti.internal"}}),
        )
        with patch("database.httpx.AsyncClient") as mock_client_cls:
            result = await db.search_component("search-comp", "anything", include_graphiti=True)
        mock_client_cls.assert_not_called()
        assert result["graphiti_error"] is None

    async def test_graphiti_error_captured_not_raised(self, db, sample_project, component_with_graphiti):
        """HTTP error from Graphiti is captured in graphiti_error, not raised."""
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("database.httpx.AsyncClient", return_value=mock_client):
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

        with patch("database.httpx.AsyncClient", return_value=mock_client):
            result = await db.search_component("search-comp", "search", include_graphiti=True)

        sources = {r["source"] for r in result["results"]}
        assert "graphiti" in sources
        assert "graphiti" in result["sources"]
