"""Smoke tests — lightweight integration tests proving key workflows work.

Tests config resolution, prompt building, task status, search, and
gate field operations using the real database module against in-memory SQLite.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


# ===========================================================================
# Config resolution
# ===========================================================================


# ===========================================================================
# Prompt building
# ===========================================================================

class TestPromptBuilding:
    """_build_task_prompt includes expected sections."""

    @pytest.fixture(autouse=True)
    def _setup_patches(self):
        self.mock_get_task = AsyncMock(return_value=None)
        self.mock_read_msgs = AsyncMock(return_value={"messages": []})
        self.mock_list_files = AsyncMock(return_value=[])
        patches = [
            patch("switchboard.db.get_task", self.mock_get_task),
            patch("switchboard.db.read_task_messages", self.mock_read_msgs),
            patch("switchboard.db.list_files", self.mock_list_files),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    def _project(self, **kw):
        base = {"id": "test-proj", "repo": "git@x.git", "test_command": "pytest"}
        base.update(kw)
        return base

    def _task(self, **kw):
        base = {"id": "test-proj/t1", "goal": "Do stuff", "branch": "t1",
                "auto_test": False, "depends_on": None}
        base.update(kw)
        return base


    async def test_prompt_includes_checklist(self):
        from switchboard.dispatch.sdk_session import _build_task_prompt
        checklist = [
            {"id": 1, "item": "Step one", "done": False},
            {"id": 2, "item": "Step two", "done": True},
        ]
        result = await _build_task_prompt(
            self._project(), self._task(), "spec", checklist=checklist)
        assert "Step one" in result
        assert "Step two" in result
        assert "⬜" in result
        assert "✅" in result


# ===========================================================================
# Task status with liveness
# ===========================================================================

class TestTaskStatusLiveness:


    async def test_task_status_includes_tags(self, db):
        await db.create_project(id="tg-proj", repo="git@x.git", working_dir="/w")
        await db.create_task(id="tg-proj/t1", project_id="tg-proj", goal="Tag test")
        await db.set_task_tags("tg-proj/t1", ["v5", "database"])

        status = await db.get_task_status("tg-proj/t1")
        assert "v5" in status["tags"]
        assert "database" in status["tags"]


# ===========================================================================
# Search task messages
# ===========================================================================

class TestSearchTaskMessages:

    async def test_search_finds_matching_content(self, db):
        await db.create_project(id="srch-proj", repo="git@x.git", working_dir="/w")
        await db.create_task(id="srch-proj/t1", project_id="srch-proj", goal="Search test")
        await db.post_task_message(
            task_id="srch-proj/t1", author="worker",
            content="Implemented the frobnicator module",
        )
        await db.post_task_message(
            task_id="srch-proj/t1", author="worker",
            content="Fixed a typo in README",
        )

        results = await db.search_task_messages("frobnicator")
        assert len(results) >= 1
        assert any("frobnicator" in r["snippet"] for r in results)


# ===========================================================================
# Gate fields queryable and updateable
# ===========================================================================


# ===========================================================================
# Convenience fixture smoke tests
# ===========================================================================

class TestConvenienceFixtures:
    """Verify the shared fixtures work correctly."""

    async def test_sample_project_fixture(self, sample_project):
        assert sample_project["id"] == "test-project"
        assert sample_project["env_overrides"]["NODE_ENV"] == "test"
        assert sample_project["model"] == "opus"


