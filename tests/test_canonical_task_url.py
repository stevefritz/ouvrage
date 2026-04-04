"""Tests for canonical task URL in API responses.

Verifies that get_task_status, list_tasks, dispatch_task, and transition_task
all include a `url` field when a base URL is available, and omit it otherwise.
"""

from unittest.mock import AsyncMock, patch

import pytest


TASK_ID = "test-project/implement-feature"
BASE_URL = "https://switchboard.example.dev"
EXPECTED_URL = f"{BASE_URL}/dashboard#/task/{TASK_ID}"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _set_base_url(url):
    """Set the request base URL in context for the current async task."""
    from switchboard.server.context import _REQUEST_BASE_URL
    _REQUEST_BASE_URL.set(url)


# ===========================================================================
# _task_url helper
# ===========================================================================

class TestTaskUrlHelper:
    """_task_url builds correct URL or returns None."""

    def test_returns_url_when_base_set(self):
        _set_base_url(BASE_URL)
        from switchboard.server.handlers.tasks import _task_url
        assert _task_url(TASK_ID) == EXPECTED_URL

    def test_returns_none_when_no_base(self):
        _set_base_url(None)
        from switchboard.server.handlers.tasks import _task_url
        assert _task_url(TASK_ID) is None

    def test_strips_trailing_slash_in_base(self):
        _set_base_url("https://switchboard.example.dev/")
        from switchboard.server.handlers.tasks import _task_url
        result = _task_url(TASK_ID)
        assert result is not None
        # _task_url strips trailing slash so no double-slash appears
        assert "//" not in result.replace("https://", "")

    def test_url_format(self):
        _set_base_url(BASE_URL)
        from switchboard.server.handlers.tasks import _task_url
        url = _task_url("my-project/some-task")
        assert url == "https://switchboard.example.dev/dashboard#/task/my-project/some-task"


# ===========================================================================
# get_task_status — slim mode
# ===========================================================================

class TestGetTaskStatusUrl:
    """get_task_status includes url when base URL is set."""

    async def test_slim_includes_url(self, db, sample_project, sample_task):
        _set_base_url(BASE_URL)
        from switchboard.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status({"task_id": TASK_ID})
        assert result.get("url") == EXPECTED_URL

    async def test_slim_omits_url_when_no_base(self, db, sample_project, sample_task):
        _set_base_url(None)
        from switchboard.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status({"task_id": TASK_ID})
        assert "url" not in result

    async def test_detail_includes_url(self, db, sample_project, sample_task):
        _set_base_url(BASE_URL)
        from switchboard.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status(
            {"task_id": TASK_ID, "include_detail": True}
        )
        assert result.get("url") == EXPECTED_URL

    async def test_detail_omits_url_when_no_base(self, db, sample_project, sample_task):
        _set_base_url(None)
        from switchboard.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status(
            {"task_id": TASK_ID, "include_detail": True}
        )
        assert "url" not in result


# ===========================================================================
# list_tasks
# ===========================================================================

class TestListTasksUrl:
    """list_tasks includes url on each task when base URL is set."""

    async def test_each_task_has_url(self, db, sample_project, sample_task):
        _set_base_url(BASE_URL)
        from switchboard.server.handlers.tasks import _handle_list_tasks
        result = await _handle_list_tasks({"project_id": "test-project", "active_only": False})
        assert len(result) > 0
        for task in result:
            expected = f"{BASE_URL}/dashboard#/task/{task['id']}"
            assert task.get("url") == expected

    async def test_no_url_when_no_base(self, db, sample_project, sample_task):
        _set_base_url(None)
        from switchboard.server.handlers.tasks import _handle_list_tasks
        result = await _handle_list_tasks({"project_id": "test-project", "active_only": False})
        for task in result:
            assert "url" not in task


# ===========================================================================
# dispatch_task
# ===========================================================================

class TestDispatchTaskUrl:
    """dispatch_task return dict includes url when base URL is set."""

    @pytest.fixture(autouse=True)
    def _patches(self, mock_git):
        pass

    async def test_dispatch_includes_url(self, db, sample_project):
        _set_base_url(BASE_URL)
        from switchboard.server.handlers.tasks import _handle_dispatch_task
        from switchboard.server.context import _REQUEST_USER_ID
        _REQUEST_USER_ID.set(None)

        # held=True → task stays in "ready", no session launched
        result = await _handle_dispatch_task({
            "project_id": "test-project",
            "id": "dispatch-url-test",
            "goal": "Test URL in dispatch response",
            "held": True,
        })

        expected_task_id = "test-project/dispatch-url-test"
        assert result.get("url") == f"{BASE_URL}/dashboard#/task/{expected_task_id}"

    async def test_dispatch_omits_url_when_no_base(self, db, sample_project):
        _set_base_url(None)
        from switchboard.server.handlers.tasks import _handle_dispatch_task
        from switchboard.server.context import _REQUEST_USER_ID
        _REQUEST_USER_ID.set(None)

        # held=True → task stays in "ready", no session launched
        result = await _handle_dispatch_task({
            "project_id": "test-project",
            "id": "dispatch-no-url-test",
            "goal": "Test no URL in dispatch response",
            "held": True,
        })

        assert "url" not in result


# ===========================================================================
# _resolve_base_url (app.py helper)
# ===========================================================================

class TestResolveBaseUrl:
    """_resolve_base_url prefers OAUTH_BASE_URL over Host header."""

    def test_uses_oauth_base_url_when_set(self):
        with patch("switchboard.server.app.OAUTH_BASE_URL", "https://configured.example.com"):
            from switchboard.server.app import _resolve_base_url
            scope = {
                "headers": [(b"host", b"runtime.example.com")],
                "scheme": "http",
            }
            assert _resolve_base_url(scope) == "https://configured.example.com"

    def test_falls_back_to_host_header(self):
        with patch("switchboard.server.app.OAUTH_BASE_URL", None):
            from switchboard.server.app import _resolve_base_url
            scope = {
                "headers": [(b"host", b"myserver.local:8080")],
                "scheme": "https",
            }
            assert _resolve_base_url(scope) == "https://myserver.local:8080"

    def test_returns_none_when_no_host_and_no_config(self):
        with patch("switchboard.server.app.OAUTH_BASE_URL", None):
            from switchboard.server.app import _resolve_base_url
            scope = {"headers": [], "scheme": "https"}
            assert _resolve_base_url(scope) is None

    def test_strips_trailing_slash_from_oauth_base_url(self):
        with patch("switchboard.server.app.OAUTH_BASE_URL", "https://configured.example.com/"):
            from switchboard.server.app import _resolve_base_url
            scope = {"headers": [], "scheme": "https"}
            assert _resolve_base_url(scope) == "https://configured.example.com"
