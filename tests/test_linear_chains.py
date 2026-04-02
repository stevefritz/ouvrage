"""Tests for linear chain enforcement — each task can have at most one dependent.

Covers:
- dispatch_task: second dependent on same parent is rejected
- dispatch_task: first dependent is allowed
- dispatch_task: no depends_on still works
- dispatch_task: re-dispatching an existing task is not blocked
- update_task: updating depends_on to an already-taken parent is rejected
- update_task: updating depends_on to a free parent is allowed
- update_task: re-setting the same parent (no-op) is allowed
"""

from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# dispatch_task linear chain enforcement
# ---------------------------------------------------------------------------

class TestDispatchTaskLinearChain:
    """dispatch_task must reject a second dependent on the same parent."""

    @pytest.fixture(autouse=True)
    def _mock_git(self):
        """Patch git/worktree ops so dispatch_task doesn't need a real repo."""
        patches = [
            patch("switchboard.dispatch.engine._run_as_worker", AsyncMock(return_value=(b"", b"", 0))),
            patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake-wt")),
            patch("switchboard.dispatch.engine.cleanup_worktree", AsyncMock()),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_first_dependent_allowed(self, db, sample_project):
        """First task with depends_on pointing to parent succeeds."""
        from switchboard.dispatch.engine import dispatch_task

        parent = await db.create_task(
            id="test-project/parent-a",
            project_id="test-project",
            goal="Parent task",
        )

        result = await dispatch_task(
            project_id="test-project",
            task_id="test-project/child-a1",
            goal="First dependent",
            depends_on=parent["id"],
            held=True,
        )
        assert result.get("task_id") == "test-project/child-a1"
        # Verify depends_on is stored in DB
        child = await db.get_task("test-project/child-a1")
        assert child["depends_on"] == parent["id"]

    async def test_second_dependent_rejected(self, db, sample_project):
        """A second task trying to depend on the same parent is rejected."""
        from switchboard.dispatch.engine import dispatch_task

        parent = await db.create_task(
            id="test-project/parent-b",
            project_id="test-project",
            goal="Parent task",
        )
        # First dependent — should succeed
        await dispatch_task(
            project_id="test-project",
            task_id="test-project/child-b1",
            goal="First dependent",
            depends_on=parent["id"],
            held=True,
        )

        # Second dependent — must be rejected
        with pytest.raises(ValueError, match="already has a dependent"):
            await dispatch_task(
                project_id="test-project",
                task_id="test-project/child-b2",
                goal="Second dependent — should fail",
                depends_on=parent["id"],
                held=True,
            )

    async def test_error_message_contains_expected_info(self, db, sample_project):
        """Error message names the existing dependent and states chains are linear."""
        from switchboard.dispatch.engine import dispatch_task

        parent = await db.create_task(
            id="test-project/parent-c",
            project_id="test-project",
            goal="Parent task",
        )
        await dispatch_task(
            project_id="test-project",
            task_id="test-project/child-c1",
            goal="First dependent",
            depends_on=parent["id"],
            held=True,
        )

        with pytest.raises(ValueError) as exc_info:
            await dispatch_task(
                project_id="test-project",
                task_id="test-project/child-c2",
                goal="Second dependent",
                depends_on=parent["id"],
                held=True,
            )

        error = str(exc_info.value)
        assert "test-project/child-c1" in error
        assert "test-project/parent-c" in error
        assert "Chains cannot fork" in error

    async def test_no_depends_on_still_works(self, db, sample_project):
        """Tasks without depends_on are not affected by the constraint."""
        from switchboard.dispatch.engine import dispatch_task

        result = await dispatch_task(
            project_id="test-project",
            task_id="test-project/standalone-task",
            goal="No dependency",
            held=True,
        )
        assert result.get("task_id") == "test-project/standalone-task"

    async def test_redispatch_existing_task_not_blocked(self, db, sample_project):
        """Re-dispatching an existing task (task already in DB) is not blocked by the check."""
        from switchboard.dispatch.engine import dispatch_task

        parent = await db.create_task(
            id="test-project/parent-d",
            project_id="test-project",
            goal="Parent task",
        )
        # Create child directly in DB (bypassing dispatch_task fan-out check)
        child = await db.create_task(
            id="test-project/child-d1",
            project_id="test-project",
            goal="Child task",
            depends_on=parent["id"],
        )

        # Re-dispatch the same child — task already exists in DB, check is skipped
        result = await dispatch_task(
            project_id="test-project",
            task_id=child["id"],
            goal=child["goal"],
            depends_on=parent["id"],
            held=True,
        )
        assert result.get("task_id") == child["id"]


# ---------------------------------------------------------------------------
# update_task depends_on linear chain enforcement
# ---------------------------------------------------------------------------

class TestUpdateTaskLinearChain:
    """update_task must reject updating depends_on to a parent that already has a dependent."""

    @pytest.fixture(autouse=True)
    def _set_context(self):
        """Set request context for handler calls (simulates non-worker, non-token request)."""
        from switchboard.server.context import set_request_context
        set_request_context(user_id=None, is_token_auth=False, is_worker=False)

    async def test_update_depends_on_to_free_parent_allowed(self, db, sample_project):
        """Updating depends_on to a parent with no dependents succeeds."""
        from switchboard.server.handlers.tasks import _handle_update_task

        parent = await db.create_task(
            id="test-project/upd-parent-a",
            project_id="test-project",
            goal="Parent",
        )
        child = await db.create_task(
            id="test-project/upd-child-a",
            project_id="test-project",
            goal="Child",
        )

        result = await _handle_update_task({
            "task_id": child["id"],
            "depends_on": parent["id"],
        })

        assert result["depends_on"] == parent["id"]

    async def test_update_depends_on_to_taken_parent_rejected(self, db, sample_project):
        """Updating depends_on to a parent that already has a dependent is rejected."""
        from switchboard.server.handlers.tasks import _handle_update_task

        parent = await db.create_task(
            id="test-project/upd-parent-b",
            project_id="test-project",
            goal="Parent",
        )
        await db.create_task(
            id="test-project/upd-child-b1",
            project_id="test-project",
            goal="Existing child",
            depends_on=parent["id"],
        )
        new_child = await db.create_task(
            id="test-project/upd-child-b2",
            project_id="test-project",
            goal="New child — wants to take same parent",
        )

        with pytest.raises(ValueError, match="already has a dependent"):
            await _handle_update_task({
                "task_id": new_child["id"],
                "depends_on": parent["id"],
            })

    async def test_update_resetting_same_parent_allowed(self, db, sample_project):
        """Re-setting a task's depends_on to its current parent is allowed (not fan-out)."""
        from switchboard.server.handlers.tasks import _handle_update_task

        parent = await db.create_task(
            id="test-project/upd-parent-c",
            project_id="test-project",
            goal="Parent",
        )
        child = await db.create_task(
            id="test-project/upd-child-c",
            project_id="test-project",
            goal="Child",
            depends_on=parent["id"],
        )

        # Re-setting same parent — should not be rejected as fan-out
        result = await _handle_update_task({
            "task_id": child["id"],
            "depends_on": parent["id"],
        })

        assert result["depends_on"] == parent["id"]
