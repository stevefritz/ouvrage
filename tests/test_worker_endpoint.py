"""Tests for the /mcp/worker endpoint field-level restrictions.

Worker requests (is_worker=True) must not be able to modify gate fields like
auto_test, auto_review, model, etc. Safe fields like tags, jira_ticket,
conversation_id, and claude_chat_url must still work.
"""

import pytest
from unittest.mock import patch, AsyncMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_worker_context():
    from switchboard.server.context import set_request_context
    set_request_context(user_id=None, is_token_auth=False, is_worker=True)


def _set_user_context(user_id=1):
    from switchboard.server.context import set_request_context
    set_request_context(user_id=user_id, is_token_auth=True, is_worker=False)


def _clear_context():
    from switchboard.server.context import set_request_context
    set_request_context(user_id=None, is_token_auth=False, is_worker=False)


# ---------------------------------------------------------------------------
# Context var tests
# ---------------------------------------------------------------------------

class TestIsWorkerContextVar:

    def test_default_is_false(self):
        from switchboard.server.context import get_request_is_worker, set_request_context
        set_request_context(user_id=None, is_token_auth=False)
        assert get_request_is_worker() is False

    def test_worker_endpoint_sets_true(self):
        from switchboard.server.context import get_request_is_worker
        _set_worker_context()
        assert get_request_is_worker() is True

    def test_user_endpoint_stays_false(self):
        from switchboard.server.context import get_request_is_worker
        _set_user_context()
        assert get_request_is_worker() is False

    def teardown_method(self, _):
        _clear_context()


# ---------------------------------------------------------------------------
# Worker field restriction — update_task handler
# ---------------------------------------------------------------------------

class TestWorkerUpdateTaskRestrictions:

    def teardown_method(self, _):
        _clear_context()

    async def test_worker_blocked_from_auto_test(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_worker_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "auto_test": False,
        })
        assert "error" in result
        assert "auto_test" in result["error"]

    async def test_worker_blocked_from_auto_review(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_worker_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "auto_review": False,
        })
        assert "error" in result
        assert "auto_review" in result["error"]

    async def test_worker_blocked_from_auto_merge(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_worker_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "auto_merge": True,
        })
        assert "error" in result
        assert "auto_merge" in result["error"]

    async def test_worker_blocked_from_auto_pr(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_worker_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "auto_pr": True,
        })
        assert "error" in result
        assert "auto_pr" in result["error"]

    async def test_worker_blocked_from_model_change(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_worker_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "model": "opus",
        })
        assert "error" in result
        assert "model" in result["error"]

    async def test_worker_blocked_from_base_branch(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_worker_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "base_branch": "main",
        })
        assert "error" in result
        assert "base_branch" in result["error"]

    async def test_worker_blocked_from_held(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_worker_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "held": False,
        })
        assert "error" in result

    async def test_worker_blocked_multiple_fields_reported(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_worker_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "auto_test": False,
            "auto_review": False,
        })
        assert "error" in result
        assert "auto_review" in result["error"]
        assert "auto_test" in result["error"]


# ---------------------------------------------------------------------------
# Worker allowed fields — update_task handler
# ---------------------------------------------------------------------------

class TestWorkerAllowedFields:

    def teardown_method(self, _):
        _clear_context()

    async def test_worker_can_update_tags(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_worker_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "tags": ["worker-tag", "progress"],
        })
        assert "error" not in result

    async def test_worker_can_update_jira_ticket(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_worker_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "jira_ticket": "PROJ-123",
        })
        assert "error" not in result
        assert result["jira_ticket"] == "PROJ-123"

    async def test_worker_can_update_conversation_id(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_worker_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "conversation_id": "my-conv",
        })
        assert "error" not in result
        assert result["conversation_id"] == "my-conv"

    async def test_worker_can_update_claude_chat_url(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_worker_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "claude_chat_url": "https://claude.ai/chat/abc123",
        })
        assert "error" not in result
        assert result["claude_chat_url"] == "https://claude.ai/chat/abc123"


# ---------------------------------------------------------------------------
# Non-worker (human user) can still modify gate fields
# ---------------------------------------------------------------------------

class TestHumanUserUnrestricted:

    def teardown_method(self, _):
        _clear_context()

    async def test_human_can_disable_auto_test(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_user_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "auto_test": False,
        })
        assert "error" not in result
        assert result["auto_test"] == 0

    async def test_human_can_change_model(self, db, sample_task):
        from switchboard.server.handlers.tasks import _handle_update_task
        _set_user_context()
        result = await _handle_update_task({
            "task_id": sample_task["id"],
            "model": "opus",
        })
        assert "error" not in result


# ---------------------------------------------------------------------------
# Dispatch URL — worker endpoint used for CC sessions
# ---------------------------------------------------------------------------

class TestDispatchUrlIsWorkerEndpoint:

    def test_sdk_session_uses_worker_endpoint(self):
        """The MCP URL injected into CC must be /mcp/worker, not /mcp."""
        import inspect
        import switchboard.dispatch.sdk_session as sdk_mod
        source = inspect.getsource(sdk_mod)
        assert "/mcp/worker" in source, "CC dispatch must use /mcp/worker endpoint"
