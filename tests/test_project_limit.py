"""Tests for project limit enforcement.

Covers:
- count_projects() DB function
- get_max_projects() reads env var and DB runtime override
- create_project handler succeeds when under limit
- create_project handler rejects when at limit with count/max in error message
- MAX_PROJECTS=0 means unlimited (no enforcement)
- Runtime config override (from /internal/config) takes precedence over env var
- is_over_project_limit() utility
- _dispatch_launch_session blocks when over project limit (task stays ready, no queued_at)
- _handle_dispatch_task response includes warning when over limit
- _handle_transition_task blocks start/resume/approve when over limit
- /internal/config triggers drain when max_projects changes
- /dashboard/api/system includes over_project_limit, projects_count, max_projects
- get_project_limit_blocked_tasks() DB function
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import switchboard.db as db
from switchboard.server.handlers.projects import _handle_create_project


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project_args(id_: str, n: int = 0) -> dict:
    """Minimal valid args for _handle_create_project."""
    return {
        "id": id_,
        "repo": "https://github.com/acme/widgets.git",
        "working_dir": f"/work/proj-{id_}",
        "model": "sonnet",
        "review_model": "opus",
        "auto_test": True,
        "auto_review": True,
        "auto_pr": False,
        "auto_merge": False,
        "max_turns": 100,
        "max_wall_clock": 60,
    }


# ---------------------------------------------------------------------------
# count_projects()
# ---------------------------------------------------------------------------

class TestCountProjects:

    async def test_returns_zero_when_no_projects(self, db):
        count = await db.count_projects()
        assert count == 0

    async def test_counts_after_creating_projects(self, db):
        await db.create_project(
            id="p1", repo="https://github.com/acme/a.git",
            working_dir="/work/a", model="sonnet",
        )
        assert await db.count_projects() == 1

        await db.create_project(
            id="p2", repo="https://github.com/acme/b.git",
            working_dir="/work/b", model="sonnet",
        )
        assert await db.count_projects() == 2


# ---------------------------------------------------------------------------
# get_max_projects()
# ---------------------------------------------------------------------------

class TestGetMaxProjects:

    async def test_returns_env_var_when_no_db_override(self, db):
        with patch("switchboard.db.instance_config._MAX_PROJECTS_ENV", 5):
            result = await db.get_max_projects()
        assert result == 5

    async def test_returns_zero_by_default(self, db):
        with patch("switchboard.db.instance_config._MAX_PROJECTS_ENV", 0):
            result = await db.get_max_projects()
        assert result == 0

    async def test_db_override_takes_precedence_over_env_var(self, db):
        await db.set_instance_config(max_projects=7)
        with patch("switchboard.db.instance_config._MAX_PROJECTS_ENV", 3):
            result = await db.get_max_projects()
        assert result == 7

    async def test_env_var_used_when_db_override_is_none(self, db):
        # No DB override set — row doesn't exist yet
        with patch("switchboard.db.instance_config._MAX_PROJECTS_ENV", 10):
            result = await db.get_max_projects()
        assert result == 10

    async def test_db_override_zero_returns_zero(self, db):
        await db.set_instance_config(max_projects=0)
        with patch("switchboard.db.instance_config._MAX_PROJECTS_ENV", 5):
            result = await db.get_max_projects()
        # DB says 0 (unlimited), not None — so it should use 0, not env var
        assert result == 0


# ---------------------------------------------------------------------------
# Handler enforcement — project creation
# ---------------------------------------------------------------------------

class TestCreateProjectLimitEnforcement:
    """Tests for the limit check in _handle_create_project."""

    @pytest.fixture(autouse=True)
    def mock_git(self):
        """Prevent real git/working_dir operations."""
        with patch("switchboard.server.handlers.projects.normalize_repo_url",
                   side_effect=lambda r: r), \
             patch("switchboard.server.handlers.projects.get_request_user_id",
                   return_value=None), \
             patch("switchboard.server.handlers.projects._run_project_validation",
                   new=AsyncMock(side_effect=lambda pid, proj: proj)), \
             patch("switchboard.server.handlers.projects.WORKTREE_BASE", "/work"):
            yield

    async def test_create_succeeds_when_under_limit(self, db):
        with patch("switchboard.server.handlers.projects.db.get_max_projects",
                   new=AsyncMock(return_value=3)), \
             patch("switchboard.server.handlers.projects.db.count_projects",
                   new=AsyncMock(return_value=2)):
            result = await _handle_create_project(_project_args("p1"))
        assert "error" not in result
        assert result["id"] == "p1"

    async def test_create_fails_when_at_limit(self, db):
        with patch("switchboard.server.handlers.projects.db.get_max_projects",
                   new=AsyncMock(return_value=3)), \
             patch("switchboard.server.handlers.projects.db.count_projects",
                   new=AsyncMock(return_value=3)):
            result = await _handle_create_project(_project_args("p2"))
        assert "error" in result
        assert "3/3" in result["error"]
        assert "Upgrade your plan" in result["error"]

    async def test_error_message_includes_count_and_limit(self, db):
        with patch("switchboard.server.handlers.projects.db.get_max_projects",
                   new=AsyncMock(return_value=10)), \
             patch("switchboard.server.handlers.projects.db.count_projects",
                   new=AsyncMock(return_value=10)):
            result = await _handle_create_project(_project_args("p3"))
        assert "10/10" in result["error"]

    async def test_zero_limit_means_unlimited(self, db):
        # MAX_PROJECTS=0 should never call count_projects
        mock_count = AsyncMock(return_value=9999)
        with patch("switchboard.server.handlers.projects.db.get_max_projects",
                   new=AsyncMock(return_value=0)), \
             patch("switchboard.server.handlers.projects.db.count_projects",
                   new=mock_count):
            result = await _handle_create_project(_project_args("p4"))
        # Should not have hit the limit check at all
        mock_count.assert_not_called()
        assert "error" not in result

    async def test_create_fails_when_exceeding_limit(self, db):
        # count > max should also be rejected (defensive)
        with patch("switchboard.server.handlers.projects.db.get_max_projects",
                   new=AsyncMock(return_value=2)), \
             patch("switchboard.server.handlers.projects.db.count_projects",
                   new=AsyncMock(return_value=5)):
            result = await _handle_create_project(_project_args("p5"))
        assert "error" in result
        assert "5/2" in result["error"]


# ---------------------------------------------------------------------------
# Runtime override integration
# ---------------------------------------------------------------------------

class TestRuntimeOverride:
    """Runtime config (DB) overrides env var for max_projects."""

    async def test_runtime_override_takes_precedence(self, db):
        # Set DB override to 2
        await db.set_instance_config(max_projects=2)
        # Env var says 10
        with patch("switchboard.db.instance_config._MAX_PROJECTS_ENV", 10):
            result = await db.get_max_projects()
        # Should use DB value (2), not env var (10)
        assert result == 2

    async def test_env_var_used_after_db_cleared(self, db):
        # Set DB override, then clear it
        await db.set_instance_config(max_projects=5)
        await db.set_instance_config(max_projects=None)
        with patch("switchboard.db.instance_config._MAX_PROJECTS_ENV", 3):
            result = await db.get_max_projects()
        # DB value is None → fall back to env var
        assert result == 3


# ---------------------------------------------------------------------------
# is_over_project_limit() utility
# ---------------------------------------------------------------------------

class TestIsOverProjectLimit:

    async def test_not_over_when_unlimited(self, db):
        from switchboard.dispatch.internals import is_over_project_limit
        with patch("switchboard.dispatch.internals.db.get_max_projects",
                   new=AsyncMock(return_value=0)), \
             patch("switchboard.dispatch.internals.db.count_projects",
                   new=AsyncMock(return_value=999)):
            over, count, limit = await is_over_project_limit()
        assert over is False
        assert limit == 0

    async def test_not_over_when_under_limit(self, db):
        from switchboard.dispatch.internals import is_over_project_limit
        with patch("switchboard.dispatch.internals.db.get_max_projects",
                   new=AsyncMock(return_value=5)), \
             patch("switchboard.dispatch.internals.db.count_projects",
                   new=AsyncMock(return_value=3)):
            over, count, limit = await is_over_project_limit()
        assert over is False
        assert count == 3
        assert limit == 5

    async def test_over_when_exceeding_limit(self, db):
        from switchboard.dispatch.internals import is_over_project_limit
        with patch("switchboard.dispatch.internals.db.get_max_projects",
                   new=AsyncMock(return_value=2)), \
             patch("switchboard.dispatch.internals.db.count_projects",
                   new=AsyncMock(return_value=5)):
            over, count, limit = await is_over_project_limit()
        assert over is True
        assert count == 5
        assert limit == 2

    async def test_not_over_when_exactly_at_limit(self, db):
        """Exactly at limit is NOT over (count must EXCEED max)."""
        from switchboard.dispatch.internals import is_over_project_limit
        with patch("switchboard.dispatch.internals.db.get_max_projects",
                   new=AsyncMock(return_value=3)), \
             patch("switchboard.dispatch.internals.db.count_projects",
                   new=AsyncMock(return_value=3)):
            over, count, limit = await is_over_project_limit()
        assert over is False


# ---------------------------------------------------------------------------
# Dispatch blocking when over project limit
# ---------------------------------------------------------------------------

_PROJ_ID = "limit-test-proj"


async def _seed_task(db_, task_id, status="ready", held=False, queued_at=None, depends_on=None):
    """Create project + task."""
    try:
        await db_.create_project(id=_PROJ_ID, repo="https://github.com/t/r.git",
                                 working_dir="/tmp/limit-test")
    except Exception:
        pass
    task = await db_.create_task(id=task_id, project_id=_PROJ_ID,
                                 goal="test task", depends_on=depends_on)
    updates = {}
    if status != "ready":
        updates["status"] = status
    if held:
        updates["held"] = True
    if queued_at:
        updates["queued_at"] = queued_at
    if updates:
        task = await db_.update_task(task_id, **updates)
    return await db_.get_task(task_id)


class TestDispatchBlockedByProjectLimit:
    """_dispatch_launch_session blocks when over project limit."""

    @pytest.fixture(autouse=True)
    def setup_mocks(self, mock_git, mock_sdk):
        self.mock_git = mock_git
        self.mock_sdk = mock_sdk

    async def test_task_stays_ready_when_over_limit(self, db):
        """Task remains ready (no queued_at) when over project limit."""
        from switchboard.dispatch.lifecycle import lifecycle
        task_id = f"{_PROJ_ID}/blocked-task"
        await _seed_task(db, task_id)

        # Patch at source module so function-level imports get the mock
        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(True, 5, 3))):
            await lifecycle.execute(task_id, "dispatch", triggered_by="test",
                                    source_detail="test dispatch")

        task = await db.get_task(task_id)
        assert task["status"] == "ready"
        assert task["queued_at"] is None

    async def test_sdk_not_called_when_over_limit(self, db):
        """No CC session launched when over project limit."""
        from switchboard.dispatch.lifecycle import lifecycle
        task_id = f"{_PROJ_ID}/blocked-no-sdk"
        await _seed_task(db, task_id)

        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(True, 5, 3))):
            await lifecycle.execute(task_id, "dispatch", triggered_by="test",
                                    source_detail="test dispatch blocked")

        # SDK should not have been called
        assert self.mock_sdk["agent"].run.call_count == 0

    async def test_task_dispatches_when_under_limit(self, db):
        """Task proceeds to working when under project limit."""
        from switchboard.dispatch.lifecycle import lifecycle
        task_id = f"{_PROJ_ID}/under-limit-task"
        await _seed_task(db, task_id)

        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(False, 2, 3))):
            await lifecycle.execute(task_id, "dispatch", triggered_by="test",
                                    source_detail="test dispatch ok")

        task = await db.get_task(task_id)
        assert task["status"] == "working"

    async def test_blocked_task_has_no_queued_at(self, db):
        """Project-limit-blocked tasks must NOT have queued_at set (won't drain on concurrency)."""
        from switchboard.dispatch.lifecycle import lifecycle
        task_id = f"{_PROJ_ID}/no-queued-at"
        await _seed_task(db, task_id)

        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(True, 4, 2))):
            await lifecycle.execute(task_id, "dispatch", triggered_by="test",
                                    source_detail="test project limit block")

        task = await db.get_task(task_id)
        assert task["queued_at"] is None


# ---------------------------------------------------------------------------
# MCP dispatch_task response warning
# ---------------------------------------------------------------------------

class TestDispatchTaskOverLimitWarning:
    """_handle_dispatch_task includes warning when over project limit."""

    @pytest.fixture(autouse=True)
    def setup_patches(self, mock_git, mock_sdk):
        pass

    async def test_warning_in_response_when_over_limit(self, db):
        from switchboard.server.handlers.tasks import _handle_dispatch_task
        await db.create_project(id=_PROJ_ID, repo="https://github.com/t/r.git",
                                working_dir="/tmp/limit-test")
        args = {
            "project_id": _PROJ_ID,
            "id": "warn-task",
            "goal": "do something",
            "held": False,
        }
        # Patch at source module — function-level imports get the mock
        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(True, 5, 3))):
            result = await _handle_dispatch_task(args)

        assert "warning" in result
        assert "5" in result["warning"]
        assert "3" in result["warning"]
        assert "⚠️" in result["warning"]

    async def test_no_warning_when_under_limit(self, db):
        from switchboard.server.handlers.tasks import _handle_dispatch_task
        try:
            await db.create_project(id=_PROJ_ID, repo="https://github.com/t/r.git",
                                    working_dir="/tmp/limit-test")
        except Exception:
            pass
        args = {
            "project_id": _PROJ_ID,
            "id": "no-warn-task",
            "goal": "do something",
            "held": False,
        }
        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(False, 2, 3))):
            result = await _handle_dispatch_task(args)

        assert "warning" not in result

    async def test_task_created_even_when_over_limit(self, db):
        """Task is created but won't run — warning is informational."""
        from switchboard.server.handlers.tasks import _handle_dispatch_task
        try:
            await db.create_project(id=_PROJ_ID, repo="https://github.com/t/r.git",
                                    working_dir="/tmp/limit-test")
        except Exception:
            pass
        args = {
            "project_id": _PROJ_ID,
            "id": "created-over-limit",
            "goal": "blocked task",
            "held": False,
        }
        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(True, 5, 3))):
            result = await _handle_dispatch_task(args)

        assert "task_id" in result
        assert "error" not in result


# ---------------------------------------------------------------------------
# MCP transition_task blocked by project limit
# ---------------------------------------------------------------------------

class TestTransitionTaskOverLimit:
    """_handle_transition_task returns error for start/resume/approve when over limit."""

    async def test_start_blocked_when_over_limit(self, db):
        from switchboard.server.handlers.tasks import _handle_transition_task
        task_id = f"{_PROJ_ID}/start-blocked"
        await _seed_task(db, task_id, status="stopped")

        # Patch at source module — function-level imports get the mock
        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(True, 5, 3))):
            result = await _handle_transition_task({"task_id": task_id, "action": "start"})

        assert "error" in result
        assert "5" in result["error"]
        assert "3" in result["error"]
        assert "⚠️" in result["error"]

    async def test_resume_blocked_when_over_limit(self, db):
        from switchboard.server.handlers.tasks import _handle_transition_task
        task_id = f"{_PROJ_ID}/resume-blocked"
        await _seed_task(db, task_id, status="stopped")

        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(True, 4, 2))):
            result = await _handle_transition_task({"task_id": task_id, "action": "resume"})

        assert "error" in result
        assert "4" in result["error"]

    async def test_approve_blocked_when_over_limit(self, db):
        from switchboard.server.handlers.tasks import _handle_transition_task
        task_id = f"{_PROJ_ID}/approve-blocked"
        await _seed_task(db, task_id, status="ready", held=True)

        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(True, 5, 3))):
            result = await _handle_transition_task({"task_id": task_id, "action": "approve"})

        assert "error" in result
        # Held flag must remain set (task stays in current state)
        task = await db.get_task(task_id)
        assert task["held"] is True or task["held"] == 1

    async def test_cancel_not_blocked(self, db):
        """cancel action is not affected by project limit."""
        from switchboard.server.handlers.tasks import _handle_transition_task
        task_id = f"{_PROJ_ID}/cancel-ok"
        await _seed_task(db, task_id, status="ready")

        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(True, 5, 3))):
            result = await _handle_transition_task({"task_id": task_id, "action": "cancel"})

        assert "error" not in result

    async def test_start_succeeds_when_under_limit(self, db, mock_git, mock_sdk):
        """start proceeds normally when under project limit."""
        from switchboard.server.handlers.tasks import _handle_transition_task
        task_id = f"{_PROJ_ID}/start-ok"
        # start requires reason == "awaiting_feedback"
        await _seed_task(db, task_id, status="stopped")
        await db.update_task(task_id, reason="awaiting_feedback")

        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(False, 2, 5))):
            result = await _handle_transition_task({"task_id": task_id, "action": "start"})

        assert "error" not in result


# ---------------------------------------------------------------------------
# get_project_limit_blocked_tasks() DB function
# ---------------------------------------------------------------------------

class TestGetProjectLimitBlockedTasks:

    async def test_returns_ready_tasks_without_queued_at(self, db):
        """Returns ready tasks with no queued_at and not held."""
        await db.create_project(id=_PROJ_ID, repo="https://github.com/t/r.git",
                                working_dir="/tmp/limit-test")
        task_id = f"{_PROJ_ID}/blocked-ready"
        await db.create_task(id=task_id, project_id=_PROJ_ID, goal="test")
        # Status ready, no queued_at, not held — project-limit-blocked

        tasks = await db.get_project_limit_blocked_tasks()
        ids = [t["id"] for t in tasks]
        assert task_id in ids

    async def test_excludes_queued_tasks(self, db):
        """Concurrency-queued tasks (queued_at IS NOT NULL) are excluded."""
        try:
            await db.create_project(id=_PROJ_ID, repo="https://github.com/t/r.git",
                                    working_dir="/tmp/limit-test")
        except Exception:
            pass
        task_id = f"{_PROJ_ID}/queued-task"
        await db.create_task(id=task_id, project_id=_PROJ_ID, goal="test")
        await db.update_task(task_id, queued_at=db.now_iso())

        tasks = await db.get_project_limit_blocked_tasks()
        ids = [t["id"] for t in tasks]
        assert task_id not in ids

    async def test_excludes_held_tasks(self, db):
        """Held tasks are excluded."""
        try:
            await db.create_project(id=_PROJ_ID, repo="https://github.com/t/r.git",
                                    working_dir="/tmp/limit-test")
        except Exception:
            pass
        task_id = f"{_PROJ_ID}/held-task"
        await db.create_task(id=task_id, project_id=_PROJ_ID, goal="test")
        await db.update_task(task_id, held=True)

        tasks = await db.get_project_limit_blocked_tasks()
        ids = [t["id"] for t in tasks]
        assert task_id not in ids

    async def test_excludes_non_ready_tasks(self, db):
        """Working/stopped/completed tasks are excluded."""
        try:
            await db.create_project(id=_PROJ_ID, repo="https://github.com/t/r.git",
                                    working_dir="/tmp/limit-test")
        except Exception:
            pass
        for status in ("working", "stopped", "completed"):
            task_id = f"{_PROJ_ID}/non-ready-{status}"
            await db.create_task(id=task_id, project_id=_PROJ_ID, goal="test")
            await db.update_task(task_id, status=status)

        tasks = await db.get_project_limit_blocked_tasks()
        for status in ("working", "stopped", "completed"):
            assert f"{_PROJ_ID}/non-ready-{status}" not in [t["id"] for t in tasks]


# ---------------------------------------------------------------------------
# _drain_project_limit_blocked() — triggered on config change
# ---------------------------------------------------------------------------

class TestDrainProjectLimitBlocked:

    async def test_drain_dispatches_blocked_tasks_when_under_limit(self, db):
        """_drain_project_limit_blocked dispatches ready tasks when under limit."""
        from switchboard.dispatch.queue import _drain_project_limit_blocked

        task_id = f"{_PROJ_ID}/drain-me"
        await _seed_task(db, task_id)

        exec_calls = []

        async def mock_execute(tid, action, **kwargs):
            exec_calls.append((tid, action))

        # Patch at source modules — function-level imports in _drain_project_limit_blocked
        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(False, 2, 5))), \
             patch("switchboard.db.get_project_limit_blocked_tasks",
                   new=AsyncMock(return_value=[{"id": task_id}])), \
             patch("switchboard.dispatch.lifecycle.lifecycle.execute",
                   side_effect=mock_execute):
            await _drain_project_limit_blocked()

        assert (task_id, "dispatch") in exec_calls

    async def test_drain_does_nothing_when_still_over_limit(self, db):
        """_drain_project_limit_blocked is a no-op if still over limit."""
        from switchboard.dispatch.queue import _drain_project_limit_blocked

        get_called = []

        async def fake_get():
            get_called.append(True)
            return []

        with patch("switchboard.dispatch.internals.is_over_project_limit",
                   new=AsyncMock(return_value=(True, 5, 3))), \
             patch("switchboard.db.get_project_limit_blocked_tasks",
                   side_effect=fake_get):
            await _drain_project_limit_blocked()

        assert not get_called


# ---------------------------------------------------------------------------
# /internal/config triggers drain when max_projects changes
# ---------------------------------------------------------------------------

class TestConfigDrainOnMaxProjectsChange:

    @pytest.fixture(autouse=True)
    def saas_mode(self):
        with patch("switchboard.internal.api.AUTH_MODE", "saas"), \
             patch("switchboard.internal.api.INTERNAL_API_TOKEN", "secret-token"):
            yield

    async def test_drain_triggered_when_max_projects_changes(self, db):
        """POST /internal/config with max_projects triggers drain tasks."""
        import asyncio
        from switchboard.internal.api import handle_request

        drain_calls = []

        async def fake_drain_queue():
            drain_calls.append("drain_queue")

        async def fake_drain_blocked():
            drain_calls.append("drain_blocked")

        async def _call(body):
            headers = [(b"authorization", b"Bearer secret-token")]
            scope = {"type": "http", "method": "POST", "path": "/internal/config",
                     "query_string": b"", "headers": headers}
            _body = json.dumps(body).encode()
            _more = False

            async def receive():
                nonlocal _more
                if not _more:
                    _more = True
                    return {"type": "http.request", "body": _body, "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}

            status = None
            async def send(msg):
                nonlocal status
                if msg["type"] == "http.response.start":
                    status = msg["status"]

            await handle_request(scope, receive, send)
            return status

        # Patch at the source module level and run create_task immediately
        with patch("switchboard.dispatch.queue._drain_queue", fake_drain_queue), \
             patch("switchboard.dispatch.queue._drain_project_limit_blocked", fake_drain_blocked), \
             patch("switchboard.internal.api.asyncio.create_task",
                   side_effect=lambda c: asyncio.ensure_future(c)):
            status = await _call({"max_projects": 5})
            # Let pending tasks run
            await asyncio.sleep(0)

        assert status == 200
        # Both drain functions should have been scheduled
        assert "drain_queue" in drain_calls
        assert "drain_blocked" in drain_calls

    async def test_drain_not_triggered_when_only_concurrency_limit_changes(self, db):
        """Drain only fires when max_projects is in the request."""
        import asyncio
        from switchboard.internal.api import handle_request

        drain_called = []

        async def fake_drain_blocked():
            drain_called.append(True)

        async def _call(body):
            headers = [(b"authorization", b"Bearer secret-token")]
            scope = {"type": "http", "method": "POST", "path": "/internal/config",
                     "query_string": b"", "headers": headers}
            _body = json.dumps(body).encode()
            _more = False

            async def receive():
                nonlocal _more
                if not _more:
                    _more = True
                    return {"type": "http.request", "body": _body, "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}

            status = None
            async def send(msg):
                nonlocal status
                if msg["type"] == "http.response.start":
                    status = msg["status"]

            await handle_request(scope, receive, send)
            return status

        with patch("switchboard.dispatch.queue._drain_project_limit_blocked", fake_drain_blocked), \
             patch("switchboard.internal.api.asyncio.create_task",
                   side_effect=lambda c: asyncio.ensure_future(c)):
            status = await _call({"concurrency_limit": 5})
            await asyncio.sleep(0)

        assert status == 200
        assert not drain_called


# ---------------------------------------------------------------------------
# /dashboard/api/system — project limit fields
# ---------------------------------------------------------------------------

class TestDashboardSystemProjectLimitFields:
    """GET /dashboard/api/system includes over_project_limit, projects_count, max_projects."""

    async def test_system_includes_project_limit_fields_when_under(self, db):
        from switchboard.dashboard.api import _handle_system

        with patch("switchboard.dashboard.api.db.get_max_projects",
                   new=AsyncMock(return_value=5)):
            # No projects created → count = 0
            status = None
            resp_body = b""

            async def send(msg):
                nonlocal status, resp_body
                if msg["type"] == "http.response.start":
                    status = msg["status"]
                elif msg["type"] == "http.response.body":
                    resp_body += msg.get("body", b"")

            await _handle_system(send)

        data = json.loads(resp_body)
        assert "over_project_limit" in data
        assert "projects_count" in data
        assert "max_projects" in data
        assert data["over_project_limit"] is False
        assert data["projects_count"] == 0
        assert data["max_projects"] == 5

    async def test_system_over_project_limit_true_when_exceeded(self, db):
        from switchboard.dashboard.api import _handle_system

        # Create 3 projects
        for i in range(3):
            await db.create_project(id=f"proj-{i}", repo=f"https://github.com/t/r{i}.git",
                                    working_dir=f"/tmp/p{i}")

        with patch("switchboard.dashboard.api.db.get_max_projects",
                   new=AsyncMock(return_value=2)):
            resp_body = b""

            async def send(msg):
                nonlocal resp_body
                if msg["type"] == "http.response.body":
                    resp_body += msg.get("body", b"")

            await _handle_system(send)

        data = json.loads(resp_body)
        assert data["over_project_limit"] is True
        assert data["projects_count"] == 3
        assert data["max_projects"] == 2

    async def test_system_unlimited_never_over_limit(self, db):
        from switchboard.dashboard.api import _handle_system

        # Create many projects
        for i in range(10):
            await db.create_project(id=f"many-proj-{i}",
                                    repo=f"https://github.com/t/r{i}.git",
                                    working_dir=f"/tmp/many{i}")

        with patch("switchboard.dashboard.api.db.get_max_projects",
                   new=AsyncMock(return_value=0)):
            resp_body = b""

            async def send(msg):
                nonlocal resp_body
                if msg["type"] == "http.response.body":
                    resp_body += msg.get("body", b"")

            await _handle_system(send)

        data = json.loads(resp_body)
        assert data["over_project_limit"] is False
        assert data["max_projects"] == 0
