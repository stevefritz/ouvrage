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

class TestConfigResolution:
    """_resolve_limit uses task > project > global fallback chain."""

    def setup_method(self):
        from ouvrage.dispatch.engine import _resolve_limit
        self.resolve = _resolve_limit

    def test_task_value_wins(self):
        assert self.resolve(100, 200, 300) == 100

    def test_project_fallback(self):
        assert self.resolve(None, 200, 300) == 200

    def test_global_fallback(self):
        assert self.resolve(None, None, 300) == 300

    def test_task_zero_is_valid(self):
        # 0 is not None, should be used
        assert self.resolve(0, 200, 300) == 0


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
            patch("ouvrage.db.get_task", self.mock_get_task),
            patch("ouvrage.db.read_task_messages", self.mock_read_msgs),
            patch("ouvrage.db.list_files", self.mock_list_files),
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

    async def test_prompt_includes_goal(self):
        from ouvrage.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(self._project(), self._task(), "The spec")
        assert "Do stuff" in result

    async def test_prompt_includes_spec(self):
        from ouvrage.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(self._project(), self._task(), "Build the widget")
        assert "Build the widget" in result

    async def test_prompt_includes_project_id(self):
        from ouvrage.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(self._project(), self._task(), None)
        assert "test-proj" in result

    async def test_prompt_includes_checklist(self):
        from ouvrage.dispatch.sdk_session import _build_task_prompt
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

    async def test_revision_prompt_includes_feedback(self):
        from ouvrage.dispatch.sdk_session import _build_task_prompt
        feedback = [{"author": "reviewer", "title": "CHANGES REQUESTED",
                      "content": "Fix the imports"}]
        result = await _build_task_prompt(
            self._project(), self._task(), "spec", review_feedback=feedback)
        assert "REVISION REQUESTED" in result
        assert "Fix the imports" in result

    async def test_auto_test_flag_in_prompt(self):
        from ouvrage.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(
            self._project(), self._task(auto_test=True), "spec")
        assert "automatically" in result.lower()

    async def test_push_instruction_present(self):
        from ouvrage.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(self._project(), self._task(), "spec")
        assert "push your branch" in result.lower()

    async def test_prompt_includes_result_summary_instruction(self):
        from ouvrage.dispatch.sdk_session import _build_task_prompt
        result = await _build_task_prompt(self._project(), self._task(), "spec")
        assert "result" in result.lower()
        assert "post_task_message" in result or "post a" in result.lower()
        assert "files modified" in result.lower() or "files created" in result.lower()
        assert "caveats" in result.lower()
        assert "5 lines" in result or "under 5" in result


# ===========================================================================
# Task status with liveness
# ===========================================================================

class TestTaskStatusLiveness:

    async def test_task_status_includes_checklist_summary(self, db):
        await db.create_project(id="st-proj", repo="git@x.git", working_dir="/w")
        await db.create_task(id="st-proj/t1", project_id="st-proj", goal="Status test")
        await db.create_checklist_items("st-proj/t1", ["A", "B", "C"])
        await db.update_checklist_item(
            (await db.get_checklist("st-proj/t1"))[0]["id"], done=True)

        status = await db.get_task_status("st-proj/t1")
        assert status["checklist_total"] == 3
        assert status["checklist_done"] == 1

    async def test_task_status_includes_recent_messages(self, db):
        await db.create_project(id="sm-proj", repo="git@x.git", working_dir="/w")
        await db.create_task(id="sm-proj/t1", project_id="sm-proj", goal="Msg test")
        await db.post_task_message(
            task_id="sm-proj/t1", author="worker", content="Progress update")

        status = await db.get_task_status("sm-proj/t1")
        assert len(status["recent_messages"]) == 1

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

    async def test_search_no_results(self, db):
        await db.create_project(id="srch2-proj", repo="git@x.git", working_dir="/w")
        await db.create_task(id="srch2-proj/t1", project_id="srch2-proj", goal="Search test")
        await db.post_task_message(
            task_id="srch2-proj/t1", author="worker", content="Hello world",
        )
        results = await db.search_task_messages("zzz_nonexistent_zzz")
        assert len(results) == 0


# ===========================================================================
# Gate fields queryable and updateable
# ===========================================================================

class TestGateFieldsSmoke:

    async def test_gate_lifecycle(self, db):
        """Full gate lifecycle: ready → testing → passed → stale → re-passed."""
        await db.create_project(id="gl-proj", repo="git@x.git", working_dir="/w")
        await db.create_task(id="gl-proj/t1", project_id="gl-proj", goal="Gate lifecycle")

        # Testing
        t = await db.update_task("gl-proj/t1", gate_status="testing")
        assert t["gate_status"] == "testing"

        # Passed
        ts = db.now_iso()
        t = await db.update_task("gl-proj/t1", gate_status="passed", gate_passed_at=ts)
        assert t["gate_status"] == "passed"
        assert t["gate_passed_at"] is not None

        # Stale (parent was re-run)
        t = await db.update_task("gl-proj/t1", gate_status="stale", gate_passed_at=None)
        assert t["gate_status"] == "stale"
        assert t["gate_passed_at"] is None

        # Re-passed
        t = await db.update_task("gl-proj/t1", gate_status="passed", gate_passed_at=db.now_iso())
        assert t["gate_status"] == "passed"

    async def test_depends_on_queryable(self, db):
        """Tasks with depends_on can be found via get_dependents."""
        await db.create_project(id="dep-proj", repo="git@x.git", working_dir="/w")
        await db.create_task(id="dep-proj/parent", project_id="dep-proj", goal="Parent")
        await db.create_task(id="dep-proj/child", project_id="dep-proj", goal="Child",
                             depends_on="dep-proj/parent")

        deps = await db.get_dependents("dep-proj/parent")
        assert len(deps) == 1
        assert deps[0]["id"] == "dep-proj/child"


# ===========================================================================
# Convenience fixture smoke tests
# ===========================================================================

class TestConvenienceFixtures:
    """Verify the shared fixtures work correctly."""

    async def test_sample_project_fixture(self, sample_project):
        assert sample_project["id"] == "test-project"
        assert sample_project["env_overrides"]["NODE_ENV"] == "test"
        assert sample_project["model"] == "opus"

    async def test_sample_task_fixture(self, sample_task, db):
        assert sample_task["status"] == "working"
        checklist = await db.get_checklist(sample_task["id"])
        assert len(checklist) == 4

    async def test_sample_conversation_fixture(self, sample_conversation, db):
        pinned = await db.get_pinned("widget-redesign")
        assert pinned is not None
        assert "Redesign Spec" in pinned["content"]

    async def test_completed_chain_fixture(self, completed_chain, db):
        chain = await db.get_chain(completed_chain["a"]["id"])
        ids = {t["id"] for t in chain}
        assert len(ids) == 3
