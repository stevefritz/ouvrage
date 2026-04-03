"""Confirm that all component and punchlist MCP tools are removed from the tool registry."""

import pytest
from switchboard.server.dispatch import TOOL_HANDLERS, _dispatch_tool


REMOVED_TOOLS = [
    "create_component",
    "update_component",
    "get_component",
    "list_components",
    "link_conversation",
    "unlink_conversation",
    "search_component",
    "pause_component",
    "resume_component",
    "stop_component",
    "add_punchlist_item",
    "list_punchlist",
    "claim_punchlist_item",
    "resolve_punchlist_item",
    "move_task",
]


class TestRemovedToolsNotRegistered:
    """Component and punchlist tools must not appear in TOOL_HANDLERS."""

    @pytest.mark.parametrize("tool_name", REMOVED_TOOLS)
    def test_tool_not_in_handlers(self, tool_name):
        assert tool_name not in TOOL_HANDLERS, (
            f"Tool '{tool_name}' should have been removed but is still in TOOL_HANDLERS"
        )

    @pytest.mark.parametrize("tool_name", REMOVED_TOOLS)
    async def test_dispatch_raises_unknown_tool(self, tool_name):
        with pytest.raises(ValueError, match="Unknown tool"):
            await _dispatch_tool(tool_name, {})


class TestRemovedToolsNotInSchema:
    """Component and punchlist tools must not appear in the TOOLS list."""

    def test_tools_list_excludes_component_tools(self):
        from switchboard.server.tools import TOOLS
        tool_names = {t.name for t in TOOLS}
        for name in REMOVED_TOOLS:
            assert name not in tool_names, (
                f"Tool '{name}' should be absent from TOOLS list but is still present"
            )

    def test_dispatch_task_has_no_component_id(self):
        from switchboard.server.tools import TOOLS
        dispatch = next(t for t in TOOLS if t.name == "dispatch_task")
        assert "component_id" not in dispatch.inputSchema.get("properties", {}), \
            "dispatch_task should not expose component_id parameter"

    def test_update_task_has_no_component_id(self):
        from switchboard.server.tools import TOOLS
        update = next(t for t in TOOLS if t.name == "update_task")
        assert "component_id" not in update.inputSchema.get("properties", {}), \
            "update_task should not expose component_id parameter"

    def test_list_tasks_has_no_component_id(self):
        from switchboard.server.tools import TOOLS
        list_tasks = next(t for t in TOOLS if t.name == "list_tasks")
        assert "component_id" not in list_tasks.inputSchema.get("properties", {}), \
            "list_tasks should not expose component_id filter"

    def test_bulk_update_tasks_has_no_component_id(self):
        from switchboard.server.tools import TOOLS
        bulk = next(t for t in TOOLS if t.name == "bulk_update_tasks")
        assert "component_id" not in bulk.inputSchema.get("properties", {}), \
            "bulk_update_tasks should not expose component_id parameter"
