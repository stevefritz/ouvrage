"""Tests for the transition_task MCP handler.

Covers:
- Valid action dispatched to lifecycle
- Invalid action (wrong state) returns error dict
- Unknown action returns error dict
- Options passthrough for close (cleanup, force_delete_branch)
- available_actions included in get_task_status (slim + detail)
"""

import pytest
from unittest.mock import AsyncMock, patch


PROJECT_ID = "transition-test-proj"


async def _seed(db, task_id, status="ready", **extra):
    """Create project + task at the given status."""
    try:
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/transition-test",
        )
    except Exception:
        pass  # already exists

    await db.create_task(id=task_id, project_id=PROJECT_ID, goal="test transition")
    updates = {}
    if status != "ready":
        updates["status"] = status
    updates.update(extra)
    if updates:
        await db.update_task(task_id, **updates)
    return await db.get_task(task_id)


# ---------------------------------------------------------------------------
# transition_task handler tests
# ---------------------------------------------------------------------------

class TestTransitionTaskHandler:
    """Tests for _handle_transition_task via the handler function directly."""

    async def test_valid_action_stop(self, db, mock_git, mock_sdk):
        """stop on a working task transitions to stopped."""
        from switchboard.server.handlers.tasks import _handle_transition_task
        task_id = f"{PROJECT_ID}/t-stop"
        await _seed(db, task_id, status="working")

        result = await _handle_transition_task({"task_id": task_id, "action": "stop"})

        assert "error" not in result, f"Expected success, got error: {result}"
        assert result["status"] == "stopped"

    async def test_valid_action_cancel(self, db, mock_git, mock_sdk):
        """cancel on a ready task transitions to cancelled."""
        from switchboard.server.handlers.tasks import _handle_transition_task
        task_id = f"{PROJECT_ID}/t-cancel"
        await _seed(db, task_id, status="ready")

        result = await _handle_transition_task({"task_id": task_id, "action": "cancel"})

        assert "error" not in result, f"Expected success, got error: {result}"
        assert result["status"] == "cancelled"

    async def test_invalid_action_wrong_state(self, db, mock_git, mock_sdk):
        """resume on a ready task (no session) returns error dict, not exception."""
        from switchboard.server.handlers.tasks import _handle_transition_task
        task_id = f"{PROJECT_ID}/t-bad-state"
        await _seed(db, task_id, status="ready")

        result = await _handle_transition_task({"task_id": task_id, "action": "resume"})

        assert "error" in result, f"Expected error, got: {result}"
        assert result["task_id"] == task_id
        assert result["action"] == "resume"

    async def test_unknown_task_returns_error(self, db, mock_git, mock_sdk):
        """Calling with a non-existent task_id returns an error dict."""
        from switchboard.server.handlers.tasks import _handle_transition_task

        result = await _handle_transition_task(
            {"task_id": "does-not/exist", "action": "stop"}
        )

        assert "error" in result

    async def test_options_passthrough_close(self, db, mock_git, mock_sdk):
        """Options dict is passed through to lifecycle.execute for close action."""
        from switchboard.server.handlers.tasks import _handle_transition_task
        from switchboard.dispatch.lifecycle import lifecycle

        task_id = f"{PROJECT_ID}/t-close-opts"
        await _seed(db, task_id, status="stopped")

        captured_ctx = {}

        original_execute = lifecycle.execute

        async def capturing_execute(tid, action, **ctx):
            if action == "close":
                captured_ctx.update(ctx)
            return await original_execute(tid, action, **ctx)

        with patch.object(lifecycle, "execute", side_effect=capturing_execute):
            result = await _handle_transition_task({
                "task_id": task_id,
                "action": "close",
                "options": {"cleanup": False, "force_delete_branch": True},
            })

        assert "error" not in result, f"Expected success, got error: {result}"
        assert captured_ctx.get("cleanup") is False
        assert captured_ctx.get("force_delete_branch") is True

    async def test_unknown_action_returns_error(self, db, mock_git, mock_sdk):
        """A completely bogus action name returns an error dict, not an exception."""
        from switchboard.server.handlers.tasks import _handle_transition_task
        task_id = f"{PROJECT_ID}/t-unknown-action"
        await _seed(db, task_id, status="working")

        result = await _handle_transition_task({"task_id": task_id, "action": "nonexistent"})

        assert "error" in result
        assert result["task_id"] == task_id
        assert result["action"] == "nonexistent"

    async def test_options_default_empty(self, db, mock_git, mock_sdk):
        """Omitting options is equivalent to passing an empty dict."""
        from switchboard.server.handlers.tasks import _handle_transition_task
        task_id = f"{PROJECT_ID}/t-no-opts"
        await _seed(db, task_id, status="working")

        # Should not raise or error even without options key
        result = await _handle_transition_task({"task_id": task_id, "action": "stop"})
        assert "error" not in result


# ---------------------------------------------------------------------------
# available_actions in get_task_status
# ---------------------------------------------------------------------------

class TestGetTaskStatusAvailableActions:
    """get_task_status must include available_actions in both slim and detail responses."""

    async def test_slim_response_includes_available_actions(self, db):
        """Slim (default) response includes available_actions list."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        task_id = f"{PROJECT_ID}/ts-slim"
        await _seed(db, task_id, status="working")

        result = await _handle_get_task_status({"task_id": task_id})

        assert "available_actions" in result
        assert isinstance(result["available_actions"], list)
        # working state has stop and cancel actions
        action_names = [a["name"] for a in result["available_actions"]]
        assert "stop" in action_names

    async def test_detail_response_includes_available_actions(self, db):
        """Detail response also includes available_actions list."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        task_id = f"{PROJECT_ID}/ts-detail"
        await _seed(db, task_id, status="stopped", reason="paused_by_user", session_id="ses-123")

        result = await _handle_get_task_status({
            "task_id": task_id,
            "include_detail": True,
        })

        assert "available_actions" in result
        assert isinstance(result["available_actions"], list)
        action_names = [a["name"] for a in result["available_actions"]]
        assert "resume" in action_names

    async def test_available_actions_empty_for_cancelled(self, db):
        """Cancelled tasks have no available user actions (or only retry/resume)."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
        task_id = f"{PROJECT_ID}/ts-cancelled"
        await _seed(db, task_id, status="cancelled")

        result = await _handle_get_task_status({"task_id": task_id})

        assert "available_actions" in result
        # cancelled tasks may have retry/resume but never stop/cancel
        action_names = [a["name"] for a in result["available_actions"]]
        assert "stop" not in action_names
        assert "cancel" not in action_names
