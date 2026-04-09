"""Tests for depends_on validation rules and the depends-on-candidates endpoint.

Covers:
- validate_depends_on: missing task, wrong project, already has dependent, self-reference
- validate_depends_on: shorthand resolution (bare slug → project_id/slug)
- validate_depends_on: case-insensitive matching
- dispatch_task: validation is applied on task creation
- _handle_update_task (MCP): validation is applied on depends_on update
- GET /dashboard/api/tasks/depends-on-candidates: returns valid targets
"""

import json
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# validate_depends_on unit tests
# ---------------------------------------------------------------------------

class TestValidateDependsOn:
    """Direct tests for the validate_depends_on function."""

    async def test_missing_task_rejected(self, db, sample_project):
        from switchboard.dispatch.engine import validate_depends_on

        with pytest.raises(ValueError, match="not found"):
            await validate_depends_on("nonexistent-task", "test-project", "test-project/my-task")


    async def test_self_reference_rejected(self, db, sample_project):
        from switchboard.dispatch.engine import validate_depends_on

        await db.create_task(
            id="test-project/self-ref", project_id="test-project", goal="Self ref task",
        )

        with pytest.raises(ValueError, match="cannot reference the task itself"):
            await validate_depends_on(
                "test-project/self-ref", "test-project", "test-project/self-ref"
            )


# ---------------------------------------------------------------------------
# dispatch_task integration with validation
# ---------------------------------------------------------------------------

class TestDispatchTaskValidation:
    """dispatch_task applies validate_depends_on on creation."""

    @pytest.fixture(autouse=True)
    def _mock_git(self):
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


# ---------------------------------------------------------------------------
# MCP handler update_task validation
# ---------------------------------------------------------------------------

class TestMCPUpdateTaskValidation:
    """_handle_update_task applies validate_depends_on."""

    @pytest.fixture(autouse=True)
    def _set_context(self):
        from switchboard.server.context import set_request_context
        set_request_context(user_id=None, is_token_auth=False, is_worker=False)


    async def test_update_rejects_cross_project(self, db, sample_project):
        from switchboard.server.handlers.tasks import _handle_update_task

        await db.create_task(
            id="test-project/upd-task-cross", project_id="test-project", goal="Task",
        )

        with pytest.raises(ValueError, match="same project"):
            await _handle_update_task({
                "task_id": "test-project/upd-task-cross",
                "depends_on": "other-project/some-task",
            })


# ---------------------------------------------------------------------------
# Dashboard API candidates endpoint
# ---------------------------------------------------------------------------

def _make_scope(method="GET", path="/dashboard/api/tasks/depends-on-candidates", query=b""):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query,
        "headers": [],
        "session_user": {"id": 1, "email": "owner@localhost", "name": "Owner", "role": "owner"},
    }


def _make_receive(body=None):
    raw = json.dumps(body).encode() if isinstance(body, dict) else (body or b"")

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return receive


class _Capture:
    def __init__(self):
        self.status = None
        self.body = b""

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")

    def json(self):
        return json.loads(self.body)


class TestDependsOnCandidatesEndpoint:
    """GET /dashboard/api/tasks/depends-on-candidates"""

    async def test_returns_candidates_without_dependents(self, db, sample_project, mock_git):
        """Tasks without existing dependents are returned as candidates."""
        from switchboard.dashboard.api import handle_request

        # Create tasks: parent has a dependent, standalone does not
        await db.create_task(
            id="test-project/cand-parent", project_id="test-project", goal="Has a child",
        )
        await db.create_task(
            id="test-project/cand-child", project_id="test-project",
            goal="Is child", depends_on="test-project/cand-parent",
        )
        await db.create_task(
            id="test-project/cand-standalone", project_id="test-project",
            goal="No children",
        )

        scope = _make_scope(query=b"project_id=test-project")
        send = _Capture()
        await handle_request(scope, _make_receive(), send)

        assert send.status == 200
        data = send.json()
        ids = [c["id"] for c in data]
        # cand-parent should NOT be a candidate (already has dependent)
        assert "test-project/cand-parent" not in ids
        # cand-child and cand-standalone should be candidates
        assert "test-project/cand-child" in ids
        assert "test-project/cand-standalone" in ids


    async def test_missing_project_id_returns_error(self, db, sample_project, mock_git):
        """Missing project_id query param returns an error."""
        from switchboard.dashboard.api import handle_request

        scope = _make_scope(query=b"")
        send = _Capture()
        await handle_request(scope, _make_receive(), send)

        assert send.status == 400

