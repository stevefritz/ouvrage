"""Tests for lean API responses.

Covers: embedding stripping, get_task_status summary/detail modes,
list_tasks active_only filtering.
"""

import pytest


# ===========================================================================
# A. Embedding stripping
# ===========================================================================

class TestEmbeddingStripping:
    """_strip_embedding removes embedding field from message dicts."""

    def test_strip_embedding_removes_field(self):
        from switchboard.db._helpers import _strip_embedding
        msg = {"id": 1, "content": "hello", "embedding": b"\x00\x01\x02\x03"}
        result = _strip_embedding(msg)
        assert "embedding" not in result
        assert result["content"] == "hello"

    def test_strip_embedding_noop_when_absent(self):
        from switchboard.db._helpers import _strip_embedding
        msg = {"id": 1, "content": "hello"}
        result = _strip_embedding(msg)
        assert result == {"id": 1, "content": "hello"}

    def test_strip_embedding_modifies_in_place_and_returns(self):
        from switchboard.db._helpers import _strip_embedding
        msg = {"id": 1, "embedding": "blob"}
        returned = _strip_embedding(msg)
        assert returned is msg  # same object
        assert "embedding" not in msg

    async def test_read_task_messages_no_embedding(self, db, sample_project, sample_task):
        """read_task_messages response messages never contain embedding field."""
        await db.post_task_message(
            task_id="test-project/implement-feature",
            author="cc-worker",
            content="Progress update",
            type="progress",
        )
        result = await db.read_task_messages("test-project/implement-feature")
        for msg in result["messages"]:
            assert "embedding" not in msg

    async def test_get_task_status_recent_messages_no_embedding(self, db, sample_project, sample_task):
        """get_task_status recent_messages never contain embedding field."""
        await db.post_task_message(
            task_id="test-project/implement-feature",
            author="cc-worker",
            content="Working on it",
            type="progress",
        )
        status = await db.get_task_status("test-project/implement-feature")
        for msg in status["recent_messages"]:
            assert "embedding" not in msg

    async def test_conversation_read_no_embedding(self, db, sample_conversation):
        """read_messages (conversation) never returns embedding field."""
        result = await db.read_messages("widget-redesign")
        for msg in result["messages"]:
            assert "embedding" not in msg

    async def test_get_pinned_no_embedding(self, db, sample_conversation):
        """get_pinned never returns embedding field."""
        pinned = await db.get_pinned("widget-redesign")
        assert pinned is not None
        assert "embedding" not in pinned


# ===========================================================================
# B. get_task_status summary vs detail mode
# ===========================================================================

class TestGetTaskStatusSummaryMode:
    """Default (include_detail=False) returns slim summary."""

    async def test_summary_mode_returns_slim_keys(self, db, sample_project, sample_task):
        """Default response contains expected slim fields."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature"}
        )
        expected_keys = {
            "task_id", "status", "phase", "gate_status", "alive", "stale",
            "idle_minutes", "checklist_done", "checklist_total",
            "total_cost_usd", "pr_status", "last_message_excerpt", "last_message_at",
        }
        assert set(result.keys()) == expected_keys

    async def test_summary_mode_excludes_detail_fields(self, db, sample_project, sample_task):
        """Summary mode must not include last_test_output, resolved_config, or recent_messages."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature"}
        )
        assert "last_test_output" not in result
        assert "resolved_config" not in result
        assert "recent_messages" not in result
        assert "checklist" not in result

    async def test_summary_mode_correct_task_id(self, db, sample_project, sample_task):
        from switchboard.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature"}
        )
        assert result["task_id"] == "test-project/implement-feature"

    async def test_summary_mode_checklist_counts(self, db, sample_project, sample_task):
        """Checklist counts are correct in summary mode."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        # Mark 2 items done (sample_task has 4 items)
        checklist = await db.get_checklist("test-project/implement-feature")
        await db.update_checklist_item(checklist[0]["id"], done=True)
        await db.update_checklist_item(checklist[1]["id"], done=True)

        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature"}
        )
        assert result["checklist_done"] == 2
        assert result["checklist_total"] == 4

    async def test_summary_mode_last_message_excerpt(self, db, sample_project, sample_task):
        """last_message_excerpt is populated from most recent message."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        long_content = "This is a very long progress message. " * 10
        await db.post_task_message(
            task_id="test-project/implement-feature",
            author="cc-worker",
            content=long_content,
            type="progress",
        )
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature"}
        )
        assert result["last_message_excerpt"] is not None
        assert len(result["last_message_excerpt"]) <= 120
        assert result["last_message_at"] is not None

    async def test_summary_mode_no_messages_excerpt_is_none(self, db, sample_project, sample_task):
        """When no messages exist, excerpt and timestamp are None."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature"}
        )
        assert result["last_message_excerpt"] is None
        assert result["last_message_at"] is None


class TestGetTaskStatusDetailMode:
    """include_detail=True returns the full response."""

    async def test_detail_mode_includes_recent_messages(self, db, sample_project, sample_task):
        """Detail mode includes recent_messages."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        await db.post_task_message(
            task_id="test-project/implement-feature",
            author="cc-worker",
            content="In progress",
        )
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature", "include_detail": True}
        )
        assert "recent_messages" in result
        assert len(result["recent_messages"]) >= 1

    async def test_detail_mode_includes_checklist(self, db, sample_project, sample_task):
        """Detail mode includes full checklist."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature", "include_detail": True}
        )
        assert "checklist" in result
        assert len(result["checklist"]) == 4

    async def test_detail_mode_includes_state_definition(self, db, sample_project, sample_task):
        """Detail mode includes state_definition for dashboard rendering."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature", "include_detail": True}
        )
        assert "state_definition" in result

    async def test_detail_mode_includes_alive_and_stale(self, db, sample_project, sample_task):
        """Detail mode still includes liveness fields."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature", "include_detail": True}
        )
        assert "alive" in result
        assert "stale" in result

    async def test_detail_mode_no_embedding_in_messages(self, db, sample_project, sample_task):
        """Detail mode recent_messages never contain embedding field."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        await db.post_task_message(
            task_id="test-project/implement-feature",
            author="cc-worker",
            content="Done",
        )
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature", "include_detail": True}
        )
        for msg in result.get("recent_messages", []):
            assert "embedding" not in msg


# ===========================================================================
# C. list_tasks active_only filtering
# ===========================================================================

class TestListTasksActiveOnly:
    """active_only=True (MCP default) excludes stale/cancelled tasks."""

    async def test_active_only_excludes_cancelled(self, db, sample_project):
        """Cancelled tasks are excluded when active_only=True."""
        await db.create_task(
            id="test-project/cancelled-task",
            project_id="test-project",
            goal="This was cancelled",
        )
        await db.update_task("test-project/cancelled-task", status="cancelled")

        result = await db.list_tasks(project_id="test-project", active_only=True)
        ids = [t["id"] for t in result]
        assert "test-project/cancelled-task" not in ids

    async def test_active_only_excludes_error_pr_exhausted(self, db, sample_project):
        """Tasks with pr_status=error AND gate_retries>=max are excluded."""
        await db.create_task(
            id="test-project/stale-error",
            project_id="test-project",
            goal="PR errored and retries exhausted",
        )
        await db.update_task(
            "test-project/stale-error",
            status="needs-review",
            pr_status="error",
            gate_retries=3,
            max_gate_retries=3,
        )

        result = await db.list_tasks(project_id="test-project", active_only=True)
        ids = [t["id"] for t in result]
        assert "test-project/stale-error" not in ids

    async def test_active_only_excludes_conflict_pr_exhausted(self, db, sample_project):
        """Tasks with pr_status=conflict AND gate_retries>=max are excluded."""
        await db.create_task(
            id="test-project/stale-conflict",
            project_id="test-project",
            goal="PR conflict and retries exhausted",
        )
        await db.update_task(
            "test-project/stale-conflict",
            status="needs-review",
            pr_status="conflict",
            gate_retries=5,
            max_gate_retries=3,
        )

        result = await db.list_tasks(project_id="test-project", active_only=True)
        ids = [t["id"] for t in result]
        assert "test-project/stale-conflict" not in ids

    async def test_active_only_keeps_error_pr_under_limit(self, db, sample_project):
        """Tasks with pr_status=error but retries not exhausted are kept."""
        await db.create_task(
            id="test-project/recoverable-error",
            project_id="test-project",
            goal="PR errored but can retry",
        )
        await db.update_task(
            "test-project/recoverable-error",
            status="needs-review",
            pr_status="error",
            gate_retries=1,
            max_gate_retries=3,
        )

        result = await db.list_tasks(project_id="test-project", active_only=True)
        ids = [t["id"] for t in result]
        assert "test-project/recoverable-error" in ids

    async def test_active_only_keeps_normal_tasks(self, db, sample_project, sample_task):
        """Normal working tasks are always included."""
        result = await db.list_tasks(project_id="test-project", active_only=True)
        ids = [t["id"] for t in result]
        assert "test-project/implement-feature" in ids

    async def test_active_only_false_includes_cancelled(self, db, sample_project):
        """active_only=False includes cancelled tasks."""
        await db.create_task(
            id="test-project/cancelled-visible",
            project_id="test-project",
            goal="Should be visible with active_only=False",
        )
        await db.update_task("test-project/cancelled-visible", status="cancelled")

        result = await db.list_tasks(project_id="test-project", active_only=False)
        ids = [t["id"] for t in result]
        assert "test-project/cancelled-visible" in ids

    async def test_active_only_false_includes_exhausted_errors(self, db, sample_project):
        """active_only=False includes tasks with exhausted PR errors."""
        await db.create_task(
            id="test-project/old-error",
            project_id="test-project",
            goal="Old stale error task",
        )
        await db.update_task(
            "test-project/old-error",
            status="needs-review",
            pr_status="error",
            gate_retries=3,
            max_gate_retries=3,
        )

        result = await db.list_tasks(project_id="test-project", active_only=False)
        ids = [t["id"] for t in result]
        assert "test-project/old-error" in ids

    async def test_mcp_handler_defaults_to_active_only(self, db, sample_project):
        """The MCP handler uses active_only=True by default."""
        from switchboard.server.handlers.tasks import _handle_list_tasks
        await db.create_task(
            id="test-project/mcp-cancelled",
            project_id="test-project",
            goal="MCP should hide this",
        )
        await db.update_task("test-project/mcp-cancelled", status="cancelled")

        result = await _handle_list_tasks({"project_id": "test-project"})
        ids = [t["id"] for t in result]
        assert "test-project/mcp-cancelled" not in ids

    async def test_mcp_handler_active_only_false_shows_all(self, db, sample_project):
        """The MCP handler respects active_only=False."""
        from switchboard.server.handlers.tasks import _handle_list_tasks
        await db.create_task(
            id="test-project/mcp-cancelled-visible",
            project_id="test-project",
            goal="MCP should show this with active_only=False",
        )
        await db.update_task("test-project/mcp-cancelled-visible", status="cancelled")

        result = await _handle_list_tasks(
            {"project_id": "test-project", "active_only": False}
        )
        ids = [t["id"] for t in result]
        assert "test-project/mcp-cancelled-visible" in ids
