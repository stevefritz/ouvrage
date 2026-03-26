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

    async def test_short_message_not_truncated(self, db, sample_project, sample_task):
        """Messages under 200 chars are returned unchanged."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        short_content = "Short message."
        await db.post_task_message(
            task_id="test-project/implement-feature",
            author="cc-worker",
            content=short_content,
        )
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature", "include_detail": True}
        )
        msg = result["recent_messages"][-1]
        assert msg["content"] == short_content

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

    async def test_review_message_truncated_to_verdict_plus_first_para(self, db, sample_project, sample_task):
        """Review messages are truncated to verdict line + first paragraph."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        review_content = (
            "## CHANGES REQUESTED\n"
            "\n"
            "Please fix the test coverage.\n"
            "There are missing edge cases.\n"
            "\n"
            "Here is a long second paragraph with lots more detail that should be cut off. " * 10
        )
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
        assert "CHANGES REQUESTED" in review_msg["content"]
        # Second paragraph content should not appear
        assert "second paragraph" not in review_msg["content"]

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

    async def test_checklist_strips_updated_at(self, db, sample_project, sample_task):
        """Checklist items in include_detail mode only have id, item, done fields."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status(
            {"task_id": "test-project/implement-feature", "include_detail": True}
        )
        for item in result["checklist"]:
            assert set(item.keys()) == {"id", "item", "done"}
            assert "updated_at" not in item

    async def test_include_full_messages_bypasses_truncation(self, db, sample_project, sample_task):
        """include_full_messages=True returns full untruncated content."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        long_content = "y" * 500
        await db.post_task_message(
            task_id="test-project/implement-feature",
            author="cc-worker",
            content=long_content,
            type="progress",
        )
        result = await _handle_get_task_status(
            {
                "task_id": "test-project/implement-feature",
                "include_detail": True,
                "include_full_messages": True,
            }
        )
        msg = result["recent_messages"][-1]
        assert msg["content"] == long_content
        assert not msg["content"].endswith("…")

    async def test_include_full_messages_preserves_checklist_fields(self, db, sample_project, sample_task):
        """include_full_messages=True returns all checklist fields including updated_at."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status(
            {
                "task_id": "test-project/implement-feature",
                "include_detail": True,
                "include_full_messages": True,
            }
        )
        # updated_at should be present when include_full_messages=True
        for item in result["checklist"]:
            assert "updated_at" in item


# ===========================================================================
# E. read_task_messages with message_id
# ===========================================================================

class TestReadTaskMessageById:
    """message_id param fetches a single message with full content."""

    async def test_fetch_single_message_by_id(self, db, sample_project, sample_task):
        """Providing message_id returns that single message with full content."""
        from switchboard.server.handlers.tasks import _handle_read_task_messages
        long_content = "z" * 1000
        posted = await db.post_task_message(
            task_id="test-project/implement-feature",
            author="cc-worker",
            content=long_content,
            type="result",
        )
        msg_id = posted["id"]
        result = await _handle_read_task_messages(
            {"task_id": "test-project/implement-feature", "message_id": msg_id}
        )
        assert "message" in result
        assert result["message"]["id"] == msg_id
        assert result["message"]["content"] == long_content  # full, not truncated

    async def test_fetch_message_not_found_returns_error(self, db, sample_project, sample_task):
        """Nonexistent message_id returns an error dict."""
        from switchboard.server.handlers.tasks import _handle_read_task_messages
        result = await _handle_read_task_messages(
            {"task_id": "test-project/implement-feature", "message_id": 99999}
        )
        assert "error" in result

    async def test_message_id_bypasses_cursor(self, db, sample_project, sample_task):
        """When message_id is provided, after/last_n are ignored."""
        from switchboard.server.handlers.tasks import _handle_read_task_messages
        posted = await db.post_task_message(
            task_id="test-project/implement-feature",
            author="cc-worker",
            content="Target message",
        )
        msg_id = posted["id"]
        # Pass after=9999 which would normally return no results
        result = await _handle_read_task_messages(
            {
                "task_id": "test-project/implement-feature",
                "message_id": msg_id,
                "after": 99999,
            }
        )
        assert "message" in result
        assert result["message"]["id"] == msg_id

    async def test_without_message_id_returns_cursor_list(self, db, sample_project, sample_task):
        """Without message_id, normal cursor behavior is unchanged."""
        from switchboard.server.handlers.tasks import _handle_read_task_messages
        await db.post_task_message(
            task_id="test-project/implement-feature",
            author="cc-worker",
            content="Normal message",
        )
        result = await _handle_read_task_messages(
            {"task_id": "test-project/implement-feature"}
        )
        assert "messages" in result
        assert "cursor" in result
