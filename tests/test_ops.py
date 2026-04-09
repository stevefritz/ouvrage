"""Tests for v5 operational improvements.

Covers: claude_chat_url, state definitions, get_guide, stall detection,
resume vs retry session handling.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from switchboard.config.constants import STALL_THRESHOLD_SECONDS
from switchboard.server.handlers.ops import _handle_get_guide
from switchboard.server.handlers.tasks import _handle_get_task_status, _handle_list_tasks


# ===========================================================================
# claude_chat_url field
# ===========================================================================


# ===========================================================================
# State Definitions
# ===========================================================================

class TestStateDefinitions:
    """Custom state definitions on projects."""


    async def test_unknown_state_gets_default(self, db):
        defn = db.get_state_definition("custom:frobnicating")
        assert defn["color"] == "#6b7280"
        assert defn["label"] == "Custom:Frobnicating"
        assert defn["pulse"] is False


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


# ===========================================================================
# get_guide endpoint
# ===========================================================================

class TestGetGuide:
    """get_guide MCP tool returns structured guide."""


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


    async def test_claude_chat_url_mutable(self, db, sample_project):
        """claude_chat_url should be in TASK_MUTABLE_FIELDS."""
        assert "claude_chat_url" in db.TASK_MUTABLE_FIELDS


# ===========================================================================
# Stall detection background checker
# ===========================================================================

class TestStallChecker:
    """Background stall checker posts warnings."""


    async def test_stall_threshold_constant(self, db):
        """STALL_THRESHOLD_SECONDS should be 300 (5 minutes)."""
        assert STALL_THRESHOLD_SECONDS == 300
