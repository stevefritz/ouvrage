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


# ---------------------------------------------------------------------------
# Worker allowed fields — update_task handler
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Non-worker (human user) can still modify gate fields
# ---------------------------------------------------------------------------


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
