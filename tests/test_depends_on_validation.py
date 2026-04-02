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

    async def test_wrong_project_rejected(self, db, sample_project):
        from switchboard.dispatch.engine import validate_depends_on

        # Create a task in the right project
        await db.create_task(
            id="test-project/parent", project_id="test-project", goal="Parent",
        )

        with pytest.raises(ValueError, match="same project"):
            await validate_depends_on(
                "other-project/parent", "test-project", "test-project/my-task"
            )

    async def test_already_has_dependent_rejected(self, db, sample_project):
        from switchboard.dispatch.engine import validate_depends_on

        parent = await db.create_task(
            id="test-project/dep-parent", project_id="test-project", goal="Parent",
        )
        await db.create_task(
            id="test-project/dep-child-1", project_id="test-project",
            goal="Existing child", depends_on=parent["id"],
        )

        with pytest.raises(ValueError, match="already has a dependent"):
            await validate_depends_on(
                parent["id"], "test-project", "test-project/dep-child-2"
            )

    async def test_self_reference_rejected(self, db, sample_project):
        from switchboard.dispatch.engine import validate_depends_on

        await db.create_task(
            id="test-project/self-ref", project_id="test-project", goal="Self ref task",
        )

        with pytest.raises(ValueError, match="cannot reference the task itself"):
            await validate_depends_on(
                "test-project/self-ref", "test-project", "test-project/self-ref"
            )

    async def test_shorthand_resolved(self, db, sample_project):
        """Bare slug (no /) is prepended with project_id."""
        from switchboard.dispatch.engine import validate_depends_on

        await db.create_task(
            id="test-project/short-parent", project_id="test-project", goal="Parent",
        )

        result = await validate_depends_on(
            "short-parent", "test-project", "test-project/short-child"
        )
        assert result == "test-project/short-parent"

    async def test_case_insensitive_match(self, db, sample_project):
        """Validation is case-insensitive for comparison."""
        from switchboard.dispatch.engine import validate_depends_on

        await db.create_task(
            id="test-project/Case-Parent", project_id="test-project", goal="Parent",
        )

        # Should find it despite case difference
        result = await validate_depends_on(
            "test-project/Case-Parent", "test-project", "test-project/case-child"
        )
        assert result == "test-project/Case-Parent"

    async def test_valid_depends_on_returns_id(self, db, sample_project):
        """Happy path: valid depends_on returns the resolved task ID."""
        from switchboard.dispatch.engine import validate_depends_on

        parent = await db.create_task(
            id="test-project/valid-parent", project_id="test-project", goal="Parent",
        )

        result = await validate_depends_on(
            parent["id"], "test-project", "test-project/valid-child"
        )
        assert result == parent["id"]

    async def test_resetting_same_parent_allowed(self, db, sample_project):
        """A task can re-set depends_on to its current parent (not a fork)."""
        from switchboard.dispatch.engine import validate_depends_on

        parent = await db.create_task(
            id="test-project/resame-parent", project_id="test-project", goal="Parent",
        )
        await db.create_task(
            id="test-project/resame-child", project_id="test-project",
            goal="Child", depends_on=parent["id"],
        )

        # Re-validating same parent for the same child should succeed
        result = await validate_depends_on(
            parent["id"], "test-project", "test-project/resame-child"
        )
        assert result == parent["id"]


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

    async def test_dispatch_rejects_nonexistent_depends_on(self, db, sample_project):
        from switchboard.dispatch.engine import dispatch_task

        with pytest.raises(ValueError, match="not found"):
            await dispatch_task(
                project_id="test-project",
                task_id="test-project/new-child",
                goal="New child",
                depends_on="test-project/nonexistent",
                held=True,
            )

    async def test_dispatch_rejects_cross_project_depends_on(self, db, sample_project):
        from switchboard.dispatch.engine import dispatch_task

        with pytest.raises(ValueError, match="same project"):
            await dispatch_task(
                project_id="test-project",
                task_id="test-project/new-child",
                goal="New child",
                depends_on="other-project/some-task",
                held=True,
            )

    async def test_dispatch_rejects_self_reference(self, db, sample_project):
        from switchboard.dispatch.engine import dispatch_task

        with pytest.raises(ValueError, match="cannot reference the task itself"):
            await dispatch_task(
                project_id="test-project",
                task_id="test-project/self-ref-dispatch",
                goal="Self reference",
                depends_on="test-project/self-ref-dispatch",
                held=True,
            )

    async def test_dispatch_resolves_shorthand(self, db, sample_project):
        from switchboard.dispatch.engine import dispatch_task

        await db.create_task(
            id="test-project/short-parent-d", project_id="test-project", goal="Parent",
        )

        result = await dispatch_task(
            project_id="test-project",
            task_id="test-project/short-child-d",
            goal="Child with shorthand",
            depends_on="short-parent-d",
            held=True,
        )
        child = await db.get_task("test-project/short-child-d")
        assert child["depends_on"] == "test-project/short-parent-d"


# ---------------------------------------------------------------------------
# MCP handler update_task validation
# ---------------------------------------------------------------------------

class TestMCPUpdateTaskValidation:
    """_handle_update_task applies validate_depends_on."""

    @pytest.fixture(autouse=True)
    def _set_context(self):
        from switchboard.server.context import set_request_context
        set_request_context(user_id=None, is_token_auth=False, is_worker=False)

    async def test_update_rejects_nonexistent_depends_on(self, db, sample_project):
        from switchboard.server.handlers.tasks import _handle_update_task

        await db.create_task(
            id="test-project/upd-task", project_id="test-project", goal="Task",
        )

        with pytest.raises(ValueError, match="not found"):
            await _handle_update_task({
                "task_id": "test-project/upd-task",
                "depends_on": "test-project/nonexistent",
            })

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

    async def test_returns_id_goal_status(self, db, sample_project, mock_git):
        """Each candidate has id, goal, status fields."""
        from switchboard.dashboard.api import handle_request

        await db.create_task(
            id="test-project/cand-fields", project_id="test-project",
            goal="Check fields",
        )

        scope = _make_scope(query=b"project_id=test-project")
        send = _Capture()
        await handle_request(scope, _make_receive(), send)

        assert send.status == 200
        data = send.json()
        task_cand = next((c for c in data if c["id"] == "test-project/cand-fields"), None)
        assert task_cand is not None
        assert "goal" in task_cand
        assert "status" in task_cand

    async def test_missing_project_id_returns_error(self, db, sample_project, mock_git):
        """Missing project_id query param returns an error."""
        from switchboard.dashboard.api import handle_request

        scope = _make_scope(query=b"")
        send = _Capture()
        await handle_request(scope, _make_receive(), send)

        assert send.status == 400

    async def test_all_statuses_included(self, db, sample_project, mock_git):
        """Tasks of all statuses are included as candidates."""
        from switchboard.dashboard.api import handle_request

        await db.create_task(
            id="test-project/cand-completed", project_id="test-project",
            goal="Completed task",
        )
        await db.update_task("test-project/cand-completed", status="completed")

        scope = _make_scope(query=b"project_id=test-project")
        send = _Capture()
        await handle_request(scope, _make_receive(), send)

        assert send.status == 200
        data = send.json()
        ids = [c["id"] for c in data]
        assert "test-project/cand-completed" in ids
