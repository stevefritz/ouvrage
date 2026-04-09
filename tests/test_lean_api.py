"""Tests for lean API responses.

Covers: embedding stripping, get_task_status summary/detail modes,
list_tasks active_only filtering.
"""

import pytest


# ===========================================================================
# A. Embedding stripping
# ===========================================================================


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
            "available_actions", "files",
        }
        assert set(result.keys()) == expected_keys


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


# ===========================================================================
# C. list_tasks active_only filtering
# ===========================================================================

class TestListTasksActiveOnly:
    """active_only=True (MCP default) excludes stale/cancelled tasks."""


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


# ===========================================================================
# D. Message truncation in include_detail mode
# ===========================================================================

class TestMessageTruncation:
    """include_detail=True truncates messages by default; include_full_messages bypasses."""

    async def test_long_message_truncated_to_200_chars(self, db, sample_project, sample_task):
        """Regular messages over 200 chars are truncated with '…' appended."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        long_content = "x" * 400
        await db.post_task_message(
            task_id="test-project/implement-feature",
            author="cc-worker",
            content=long_content,
            type="progress",
        )
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature", "include_detail": True}
        )
        msg = result["recent_messages"][-1]
        assert msg["content"].endswith("…")
        assert len(msg["content"]) == 201  # 200 chars + ellipsis


    async def test_spec_pinned_message_never_truncated(self, db, sample_project, sample_task):
        """Pinned spec messages are never truncated regardless of length."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        spec_content = "# Spec\n\n" + "Detail " * 100
        await db.post_task_message(
            task_id="test-project/implement-feature",
            author="cc-worker",
            content=spec_content,
            type="spec",
            pinned=True,
        )
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature", "include_detail": True}
        )
        spec_msg = next(m for m in result["recent_messages"] if m.get("pinned"))
        assert spec_msg["content"] == spec_content


    async def test_review_message_approved_verdict_extracted(self, db, sample_project, sample_task):
        """APPROVED verdict is preserved in truncated review."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        review_content = "## APPROVED\n\nLooks good. Ship it.\n\nLong detailed analysis follows here. " * 20
        await db.post_task_message(
            task_id="test-project/implement-feature",
            author="dispatcher",
            content=review_content,
            type="review",
        )
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature", "include_detail": True}
        )
        review_msg = next(m for m in result["recent_messages"] if m.get("type") == "review")
        assert "APPROVED" in review_msg["content"]
        assert "Looks good. Ship it." in review_msg["content"]


# ===========================================================================
# E. read_task_messages with message_id
# ===========================================================================

class TestReadTaskMessageById:
    """message_id param fetches a single message with full content."""


    async def test_fetch_message_not_found_returns_error(self, db, sample_project, sample_task):
        """Nonexistent message_id returns an error dict."""
        from switchboard.server.handlers.tasks import _handle_read_task_messages
        result = await _handle_read_task_messages(
            {"task_id": "test-project/implement-feature", "message_id": 99999}
        )
        assert "error" in result


