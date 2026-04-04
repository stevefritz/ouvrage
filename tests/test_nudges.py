"""Tests for the behavioral nudge system."""

import random
from unittest.mock import patch

import pytest

from switchboard.config.nudges import (
    NUDGE_CATEGORIES,
    TOOL_CATEGORY_MAP,
    append_nudge,
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

    def test_get_context_is_excluded_from_nudges(self):
        # get_context maps to "planning" in the category map but is in the no-nudge set
        assert select_nudge("get_context") is None

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


class TestSelectNudge:
    """select_nudge() behavior."""

    def test_returns_none_for_get_guide(self):
        assert select_nudge("get_guide") is None

    def test_returns_none_for_get_context(self):
        assert select_nudge("get_context") is None

    def test_returns_string_for_regular_tool(self):
        nudge = select_nudge("dispatch_task")
        assert isinstance(nudge, str)
        assert nudge.strip()

    def test_returns_string_for_unknown_tool(self):
        # Tools not in map get baseline weights across all categories
        nudge = select_nudge("some_unknown_tool")
        assert isinstance(nudge, str)
        assert nudge.strip()

    def test_returned_nudge_is_from_nudge_pool(self):
        all_nudges = {n for nudges in NUDGE_CATEGORIES.values() for n in nudges}
        for tool in ("dispatch_task", "search", "post", "get_task_status", "list_tasks"):
            nudge = select_nudge(tool)
            assert nudge in all_nudges, f"Nudge '{nudge}' not in known pool"

    def test_boosted_category_selected_more_often(self):
        """dispatch_task boosts 'dispatch' category — dispatch nudges should dominate."""
        dispatch_nudges = set(NUDGE_CATEGORIES["dispatch"])
        hits = sum(1 for _ in range(300) if select_nudge("dispatch_task") in dispatch_nudges)
        # With 4 dispatch nudges at 3x weight vs ~19 others at 1x: expected ~300 * 12/31 ≈ 116
        # Baseline random would be ~300 * 4/23 ≈ 52. Set a conservative threshold of 70.
        assert hits > 70, f"Only {hits}/300 nudges from boosted category — weighting may be broken"

    def test_non_boosted_categories_still_appear(self):
        """Even with a boost, other categories should appear over many draws."""
        dispatch_nudges = set(NUDGE_CATEGORIES["dispatch"])
        non_dispatch = sum(
            1 for _ in range(200) if select_nudge("dispatch_task") not in dispatch_nudges
        )
        assert non_dispatch > 20, "Non-boosted categories never appeared — pool may be wrong"

    def test_nudge_selected_deterministically_with_fixed_random(self):
        """With a fixed random seed, select_nudge returns a consistent result."""
        with patch("switchboard.config.nudges.random") as mock_random:
            mock_random.random.return_value = 0.0
            nudge1 = select_nudge("search")
            nudge2 = select_nudge("search")
        assert nudge1 == nudge2


class TestAppendNudge:
    """append_nudge() integration."""

    def test_appends_nudge_separator_and_emoji(self):
        result = append_nudge('{"key":"value"}', "dispatch_task")
        assert "\n\n---\n" in result
        assert "\U0001f4a1" in result  # 💡

    def test_nudge_is_appended_after_response_text(self):
        response = '{"status":"ok"}'
        result = append_nudge(response, "search")
        assert result.startswith(response)

    def test_no_nudge_appended_for_get_guide(self):
        response = '{"guide":"..."}'
        result = append_nudge(response, "get_guide")
        assert result == response

    def test_no_nudge_appended_for_get_context(self):
        response = '{"context":"..."}'
        result = append_nudge(response, "get_context")
        assert result == response

    def test_nudge_text_is_from_known_pool(self):
        all_nudges = {n for nudges in NUDGE_CATEGORIES.values() for n in nudges}
        result = append_nudge('{}', "post")
        # Extract the nudge text after the separator
        parts = result.split("\n\n---\n\U0001f4a1 ", 1)
        assert len(parts) == 2
        nudge_text = parts[1]
        assert nudge_text in all_nudges
