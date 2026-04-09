"""Tests for the behavioral nudge system."""

import random
from unittest.mock import patch

import pytest

from switchboard.config.nudges import (
    NUDGE_CATEGORIES,
    TOOL_CATEGORY_MAP,
    inject_nudge,
    select_nudge,
)


class TestNudgeCategories:
    """NUDGE_CATEGORIES structure."""

    def test_all_six_categories_present(self):
        expected = {"planning", "dispatch", "communication", "search", "quality", "interaction"}
        assert set(NUDGE_CATEGORIES.keys()) == expected

    def test_each_category_has_at_least_three_nudges(self):
        for category, nudges in NUDGE_CATEGORIES.items():
            assert len(nudges) >= 3, f"{category} has fewer than 3 nudges"

    def test_all_nudges_are_non_empty_strings(self):
        for category, nudges in NUDGE_CATEGORIES.items():
            for nudge in nudges:
                assert isinstance(nudge, str) and nudge.strip(), f"Empty nudge in {category}"


class TestToolCategoryMap:
    """TOOL_CATEGORY_MAP entries."""

    def test_get_guide_maps_to_none(self):
        assert TOOL_CATEGORY_MAP["get_guide"] is None


    def test_dispatch_tools_map_to_dispatch(self):
        assert TOOL_CATEGORY_MAP["dispatch_task"] == "dispatch"
        assert TOOL_CATEGORY_MAP["transition_task"] == "dispatch"

    def test_search_tools_map_to_search(self):
        assert TOOL_CATEGORY_MAP["search"] == "search"
        assert TOOL_CATEGORY_MAP["read"] == "search"
        assert TOOL_CATEGORY_MAP["get_pinned"] == "search"

    def test_communication_tools_map_correctly(self):
        assert TOOL_CATEGORY_MAP["post"] == "communication"
        assert TOOL_CATEGORY_MAP["post_task_message"] == "communication"
        assert TOOL_CATEGORY_MAP["create_conversation"] == "communication"

    def test_quality_tools_map_correctly(self):
        assert TOOL_CATEGORY_MAP["get_task_status"] == "quality"
        assert TOOL_CATEGORY_MAP["get_session_log"] == "quality"
        assert TOOL_CATEGORY_MAP["get_dispatch_log"] == "quality"

    def test_planning_tools_map_correctly(self):
        assert TOOL_CATEGORY_MAP["list_tasks"] == "planning"
        assert TOOL_CATEGORY_MAP["conversations"] == "planning"
        assert TOOL_CATEGORY_MAP["get_context"] == "planning"


class TestInjectNudge:
    """inject_nudge() integration."""


    def test_no_nudge_injected_for_get_context(self):
        result = {"context": "..."}
        inject_nudge(result, "get_context")
        assert "_nudge" not in result


