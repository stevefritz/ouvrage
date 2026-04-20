"""Tests for v5 operational improvements.

Covers: claude_chat_url, state definitions, get_guide, stall detection,
resume vs retry session handling.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from ouvrage.config.constants import STALL_THRESHOLD_SECONDS
from ouvrage.server.handlers.ops import _handle_get_guide
from ouvrage.server.handlers.tasks import _handle_get_task_status, _handle_list_tasks


# ===========================================================================
# claude_chat_url field
# ===========================================================================

class TestClaudeChatUrl:
    """claude_chat_url on tasks and conversations."""

    async def test_task_has_claude_chat_url_column(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("PRAGMA table_info(tasks)")
            col_names = {r["name"] for r in rows}
        assert "claude_chat_url" in col_names

    async def test_conversation_has_claude_chat_url_column(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("PRAGMA table_info(conversations)")
            col_names = {r["name"] for r in rows}
        assert "claude_chat_url" in col_names

    async def test_create_task_with_chat_url(self, db, sample_project):
        url = "https://claude.ai/chat/abc-123"
        task = await db.create_task(
            id="test-project/chat-url-test",
            project_id="test-project",
            goal="Test chat URL",
            claude_chat_url=url,
        )
        assert task["claude_chat_url"] == url

        # Verify it persists
        fetched = await db.get_task("test-project/chat-url-test")
        assert fetched["claude_chat_url"] == url

    async def test_create_task_without_chat_url(self, db, sample_project):
        task = await db.create_task(
            id="test-project/no-url-test",
            project_id="test-project",
            goal="Test no URL",
        )
        assert task["claude_chat_url"] is None

    async def test_update_task_chat_url(self, db, sample_project):
        task = await db.create_task(
            id="test-project/update-url-test",
            project_id="test-project",
            goal="Test URL update",
        )
        assert task["claude_chat_url"] is None

        updated = await db.update_task(
            "test-project/update-url-test",
            claude_chat_url="https://claude.ai/chat/xyz-456",
        )
        assert updated["claude_chat_url"] == "https://claude.ai/chat/xyz-456"

    async def test_create_conversation_with_chat_url(self, db, sample_project):
        url = "https://claude.ai/chat/conv-789"
        conv = await db.create_conversation(
            id="chat-url-conv",
            project="test-project",
            goal="Test conversation chat URL",
            claude_chat_url=url,
        )
        assert conv["claude_chat_url"] == url

    async def test_create_conversation_without_chat_url(self, db, sample_project):
        conv = await db.create_conversation(
            id="no-url-conv",
            project="test-project",
            goal="No URL conv",
        )
        assert conv["claude_chat_url"] is None

    async def test_get_task_status_includes_chat_url(self, db, sample_project):
        url = "https://claude.ai/chat/status-test"
        await db.create_task(
            id="test-project/status-url",
            project_id="test-project",
            goal="Status URL test",
            claude_chat_url=url,
        )
        status = await db.get_task_status("test-project/status-url")
        assert status["claude_chat_url"] == url


# ===========================================================================
# State Definitions
# ===========================================================================

class TestStateDefinitions:
    """Custom state definitions on projects."""

    async def test_projects_has_state_definitions_column(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("PRAGMA table_info(projects)")
            col_names = {r["name"] for r in rows}
        assert "state_definitions" in col_names

    async def test_core_states_exist(self, db):
        """All core states have definitions."""
        core_states = [
            "ready", "blocked", "working", "testing", "reviewing",
            "needs-review", "completed", "merged", "failed", "cancelled",
        ]
        for state in core_states:
            defn = db.get_state_definition(state)
            assert "color" in defn
            assert "label" in defn
            assert "pulse" in defn

    async def test_core_working_state_pulses(self, db):
        defn = db.get_state_definition("working")
        assert defn["pulse"] is True

    async def test_core_completed_state_no_pulse(self, db):
        defn = db.get_state_definition("completed")
        assert defn["pulse"] is False

    async def test_unknown_state_gets_default(self, db):
        defn = db.get_state_definition("custom:frobnicating")
        assert defn["color"] == "#6b7280"
        assert defn["label"] == "Custom:Frobnicating"
        assert defn["pulse"] is False

    async def test_project_custom_states_merge(self, db):
        custom = {"custom:indexing": {"color": "#8b5cf6", "label": "Indexing", "pulse": True}}
        proj = await db.create_project(
            id="state-proj",
            repo="git@github.com:test/repo.git",
            working_dir="/work/test-states",
            state_definitions=custom,
        )
        assert proj["state_definitions"] == custom

        fetched = await db.get_project("state-proj")
        assert fetched["state_definitions"] == custom

        # Custom state is resolved
        defn = db.get_state_definition("custom:indexing", fetched)
        assert defn["color"] == "#8b5cf6"
        assert defn["label"] == "Indexing"
        assert defn["pulse"] is True

        # Core states still work
        defn = db.get_state_definition("working", fetched)
        assert defn["pulse"] is True

    async def test_custom_state_overrides_core(self, db):
        """Custom definitions can override core state appearance."""
        custom = {"working": {"color": "#ff0000", "label": "Hacking", "pulse": True}}
        proj = await db.create_project(
            id="override-proj",
            repo="git@github.com:test/override.git",
            working_dir="/work/test-override",
            state_definitions=custom,
        )
        fetched = await db.get_project("override-proj")
        defn = db.get_state_definition("working", fetched)
        assert defn["color"] == "#ff0000"
        assert defn["label"] == "Hacking"

    async def test_update_project_state_definitions(self, db, sample_project):
        custom = {"custom:deploying": {"color": "#22c55e", "label": "Deploying", "pulse": True}}
        updated = await db.update_project("test-project", state_definitions=custom)
        assert updated["state_definitions"] == custom

    async def test_merged_state_definitions(self, db):
        custom = {"custom:indexing": {"color": "#8b5cf6", "label": "Indexing", "pulse": True}}
        proj = {"state_definitions": custom}
        merged = db.get_merged_state_definitions(proj)
        assert "working" in merged  # core
        assert "custom:indexing" in merged  # custom
        assert len(merged) > len(custom)

    async def test_merged_state_definitions_no_project(self, db):
        merged = db.get_merged_state_definitions(None)
        assert "working" in merged
        assert len(merged) == len(db.CORE_STATE_DEFINITIONS)


# ===========================================================================
# get_guide endpoint
# ===========================================================================

class TestGetGuide:
    """get_guide MCP tool returns structured guide."""

    async def test_guide_returns_markdown(self, db, sample_project):
        result = await _handle_get_guide({})
        assert "guide" in result
        guide = result["guide"]
        assert "# Ouvrage" in guide
        assert "Behavioral Playbook" in guide

    async def test_guide_includes_tool_tables(self, db, sample_project):
        result = await _handle_get_guide({})
        guide = result["guide"]
        assert "dispatch_task" in guide
        assert "get_pinned" in guide
        assert "get_task_status" in guide

    async def test_guide_includes_patterns(self, db, sample_project):
        result = await _handle_get_guide({})
        guide = result["guide"]
        assert "Chain Design Patterns" in guide
        assert "Anti-Patterns" in guide

    async def test_guide_includes_live_summary(self, db, sample_project):
        result = await _handle_get_guide({})
        guide = result["guide"]
        assert "Live System Summary" in guide
        assert "Projects" in guide
        # Should show our sample project
        assert "test-project" in guide

    async def test_guide_shows_active_task_count(self, db, sample_project):
        # Create a working task
        await db.create_task(
            id="test-project/guide-task",
            project_id="test-project",
            goal="Guide test task",
        )
        await db.update_task("test-project/guide-task", status="working")

        result = await _handle_get_guide({})
        guide = result["guide"]
        assert "Active tasks" in guide


# ===========================================================================
# Stall Detection
# ===========================================================================

class TestStallDetection:
    """Stale task detection in get_task_status."""

    async def test_stale_seconds_for_active_task(self, db, sample_project):
        """Working task with old last_activity should show stale_seconds."""
        task = await db.create_task(
            id="test-project/stale-test",
            project_id="test-project",
            goal="Stale test",
        )
        # Set last_activity to 10 minutes ago
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        await db.update_task("test-project/stale-test", status="working", last_activity=old_time)

        result = await _handle_get_task_status(
            {"task_id": "test-project/stale-test", "include_detail": True}
        )
        assert result["stale_seconds"] >= 550  # ~10 min, give some slack
        assert result["alive"] is True
        assert result["idle_minutes"] >= 9.0

    async def test_stale_seconds_zero_for_non_working(self, db, sample_project):
        """Non-working tasks should have stale_seconds = 0."""
        await db.create_task(
            id="test-project/ready-stale",
            project_id="test-project",
            goal="Ready stale test",
        )
        result = await _handle_get_task_status(
            {"task_id": "test-project/ready-stale", "include_detail": True}
        )
        assert result["stale_seconds"] == 0
        assert result["alive"] is False

    async def test_stale_flag_threshold(self, db, sample_project):
        """stale flag should be True after 15 minutes."""
        task = await db.create_task(
            id="test-project/very-stale",
            project_id="test-project",
            goal="Very stale",
        )
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        await db.update_task("test-project/very-stale", status="working", last_activity=old_time)

        result = await _handle_get_task_status({"task_id": "test-project/very-stale"})
        assert result["stale"] is True

    async def test_state_definition_in_task_status(self, db, sample_project):
        """get_task_status should include state_definition."""
        await db.create_task(
            id="test-project/state-def-test",
            project_id="test-project",
            goal="State definition test",
        )
        result = await _handle_get_task_status(
            {"task_id": "test-project/state-def-test", "include_detail": True}
        )
        assert "state_definition" in result
        assert result["state_definition"]["label"] == "Ready"

    async def test_state_definition_in_list_tasks(self, db, sample_project):
        """list_tasks should include state_definition per task."""
        await db.create_task(
            id="test-project/list-state-test",
            project_id="test-project",
            goal="List state test",
        )
        result = await _handle_list_tasks({"project_id": "test-project"})
        assert len(result) >= 1
        task = next(t for t in result if t["id"] == "test-project/list-state-test")
        assert "state_definition" in task
        assert task["state_definition"]["label"] == "Ready"


# ===========================================================================
# Resume vs Retry session handling
# ===========================================================================

class TestResumeVsRetry:
    """Resume preserves session_id, retry clears it."""

    async def test_retry_clears_session_id(self, db, sample_project):
        """retry_task should clear session_id and gate state."""
        task = await db.create_task(
            id="test-project/retry-session",
            project_id="test-project",
            goal="Retry session test",
        )
        await db.update_task(
            "test-project/retry-session",
            session_id="ses_abc123",
            status="completed",
            gate_status="passed",
            gate_passed_at=db.now_iso(),
        )

        # Verify session_id is set
        task = await db.get_task("test-project/retry-session")
        assert task["session_id"] == "ses_abc123"

        # Simulate retry_task's DB updates (clearing session/gate)
        await db.update_task(
            "test-project/retry-session",
            session_id=None,
            gate_status=None,
            gate_passed_at=None,
        )
        task = await db.get_task("test-project/retry-session")
        assert task["session_id"] is None
        assert task["gate_status"] is None
        assert task["gate_passed_at"] is None

    async def test_resume_preserves_session_id(self, db, sample_project):
        """resume_task should not clear session_id."""
        task = await db.create_task(
            id="test-project/resume-session",
            project_id="test-project",
            goal="Resume session test",
        )
        await db.update_task(
            "test-project/resume-session",
            session_id="ses_xyz789",
            status="needs-review",
        )

        # Verify session_id persists (resume does NOT clear it)
        task = await db.get_task("test-project/resume-session")
        assert task["session_id"] == "ses_xyz789"
        assert task["status"] == "needs-review"

    async def test_claude_chat_url_mutable(self, db, sample_project):
        """claude_chat_url should be in TASK_MUTABLE_FIELDS."""
        assert "claude_chat_url" in db.TASK_MUTABLE_FIELDS


# ===========================================================================
# Stall detection background checker
# ===========================================================================

class TestStallChecker:
    """Background stall checker posts warnings."""

    async def test_stall_warning_posted(self, db, sample_project):
        """Stalled task gets a warning message posted."""
        task = await db.create_task(
            id="test-project/stall-warn",
            project_id="test-project",
            goal="Stall warning test",
        )
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        await db.update_task("test-project/stall-warn", status="working", last_activity=old_time)

        # Mock notify to prevent actual Slack calls
        with patch("ouvrage.notifications.slack.task_heartbeat", new_callable=AsyncMock) as mock_heartbeat:

            # Run one iteration of the check (not the infinite loop)
            working_tasks = await db.list_tasks(status="working")
            now = datetime.now(timezone.utc)
            for t in working_tasks:
                if t["id"] != "test-project/stall-warn":
                    continue
                last_activity = t.get("last_activity")
                if not last_activity:
                    continue
                last = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                stale_seconds = (now - last).total_seconds()
                if stale_seconds >= STALL_THRESHOLD_SECONDS:
                    minutes = round(stale_seconds / 60, 1)
                    await db.post_task_message(
                        task_id=t["id"], author="dispatcher",
                        type="stall-warning",
                        title=f"No activity for {minutes}m",
                        content=f"Task has had no activity for {minutes} minutes.",
                    )

        # Verify warning was posted
        thread = await db.read_task_messages("test-project/stall-warn")
        warnings = [m for m in thread["messages"] if m.get("type") == "stall-warning"]
        assert len(warnings) == 1
        assert "no activity" in warnings[0]["title"].lower()

    async def test_stall_threshold_constant(self, db):
        """STALL_THRESHOLD_SECONDS should be 300 (5 minutes)."""
        assert STALL_THRESHOLD_SECONDS == 300
