"""Tests for the TaskLifecycle service.

Uses real in-memory SQLite DB via the `db` fixture. Tests state transitions
through the service interface — no mocking of db.update_task.
"""

import asyncio

import pytest

from switchboard.dispatch.lifecycle import (
    IllegalTransition,
    TaskLifecycle,
    TransitionDef,
    TRANSITIONS,
    STATE_LABELS,
    _STATUS_MAP,
    _STATE_FALLBACKS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PROJECT_ID = "lifecycle-test-proj"
TASK_ID = "lifecycle-test-proj/task-1"


async def _seed(db, status="ready", gate_status=None, reason=None):
    """Create a project + task at the given status."""
    try:
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )
    except Exception:
        pass  # already exists

    task = await db.create_task(
        id=TASK_ID, project_id=PROJECT_ID, goal="test lifecycle",
    )
    updates = {"status": status}
    if gate_status is not None:
        updates["gate_status"] = gate_status
    if reason is not None:
        updates["reason"] = reason
    if status != "ready":
        task = await db.update_task(TASK_ID, **updates)
    elif gate_status or reason:
        task = await db.update_task(TASK_ID, **updates)
    return task


# ---------------------------------------------------------------------------
# TransitionDef unit tests
# ---------------------------------------------------------------------------

class TestTransitionDef:


    def test_defaults(self):
        td = TransitionDef(to_state="working")
        assert td.preconditions == []
        assert td.side_effects == []
        assert td.label == ""
        assert td.style == "secondary"
        assert td.confirm is False


# ---------------------------------------------------------------------------
# IllegalTransition tests
# ---------------------------------------------------------------------------

class TestIllegalTransition:


    def test_is_value_error(self):
        assert issubclass(IllegalTransition, ValueError)


# ---------------------------------------------------------------------------
# _effective_state tests
# ---------------------------------------------------------------------------

class TestEffectiveState:
    def setup_method(self):
        self.lifecycle = TaskLifecycle()


    def test_turns_exhausted_no_gates_maps_to_stopped(self):
        task = {"status": "turns-exhausted", "gate_status": None}
        assert self.lifecycle._effective_state(task) == "stopped"


    def test_unknown_status_passes_through(self):
        task = {"status": "some-future-status"}
        assert self.lifecycle._effective_state(task) == "some-future-status"


# ---------------------------------------------------------------------------
# execute() — valid transitions
# ---------------------------------------------------------------------------

class TestExecuteValidTransitions:
    """Test every transition in the table via execute() with real DB."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def _make_task(self, task_id, status="ready", gate_status=None, reason=None, **extra):
        task = await self.db.create_task(
            id=task_id, project_id=PROJECT_ID, goal="test",
        )
        updates = {}
        if status != "ready":
            updates["status"] = status
        if gate_status is not None:
            updates["gate_status"] = gate_status
        if reason is not None:
            updates["reason"] = reason
        updates.update(extra)
        if updates:
            task = await self.db.update_task(task_id, **updates)
        return task

    # --- User actions ---


    async def test_stopped_retry(self):
        await self._make_task("t/9", status="stopped")
        result = await self.lifecycle.execute("t/9", "retry")
        assert result["status"] == "working"

    async def test_stopped_start(self):
        await self._make_task("t/10", status="stopped", reason="awaiting_feedback")
        result = await self.lifecycle.execute("t/10", "start")
        assert result["status"] == "working"


    async def test_cancelled_resume(self):
        await self._make_task("t/16", status="cancelled", session_id="ses-456")
        result = await self.lifecycle.execute("t/16", "resume")
        assert result["status"] == "working"

    # --- System actions ---

    async def test_working_complete(self):
        from unittest.mock import AsyncMock, patch
        await self._make_task("t/17", status="working", worktree_path="/tmp/test-wt")
        with patch("switchboard.dispatch.gates._dispatch_review", new_callable=AsyncMock):
            result = await self.lifecycle.execute("t/17", "complete")
        assert result["status"] == "validating"


    # --- Recovery ---


    # --- Audit log ---


    # --- Reason clearing ---


# ---------------------------------------------------------------------------
# Attempt finalization
# ---------------------------------------------------------------------------

class TestAttemptFinalization:
    """Transitions out of working/validating must finalize the current attempt."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def _make_working_task(self, task_id, attempt=1):
        task = await self.db.create_task(id=task_id, project_id=PROJECT_ID, goal="test")
        await self.db.update_task(task_id, status="working", current_attempt=attempt)
        await self.db.create_attempt(task_id, attempt)
        return task

    async def _get_attempt(self, task_id, attempt=1):
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT * FROM task_attempts WHERE task_id = ? AND attempt_number = ?",
                (task_id, attempt),
            )
            return dict(rows[0]) if rows else None

    async def test_error_finalizes_attempt(self):
        await self._make_working_task("t/fin-1")
        await self.lifecycle.execute("t/fin-1", "error")
        attempt = await self._get_attempt("t/fin-1")
        assert attempt["finished_at"] is not None
        assert attempt["outcome"] == "dispatch_error"


# ---------------------------------------------------------------------------
# execute() — illegal transitions
# ---------------------------------------------------------------------------

class TestExecuteIllegalTransitions:
    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def _make_task(self, task_id, status="ready"):
        task = await self.db.create_task(
            id=task_id, project_id=PROJECT_ID, goal="test",
        )
        if status != "ready":
            task = await self.db.update_task(task_id, status=status)
        return task


# ---------------------------------------------------------------------------
# execute() with preconditions and side effects
# ---------------------------------------------------------------------------

class TestPreconditionsAndSideEffects:
    @pytest.fixture(autouse=True)
    async def _setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )


# ---------------------------------------------------------------------------
# get_available_actions tests
# ---------------------------------------------------------------------------

class TestGetAvailableActions:
    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def _make_task(self, task_id, status="ready"):
        task = await self.db.create_task(
            id=task_id, project_id=PROJECT_ID, goal="test",
        )
        if status != "ready":
            task = await self.db.update_task(task_id, status=status)
        return task


    async def test_task_not_found_raises(self):
        with pytest.raises(ValueError, match="not found"):
            await self.lifecycle.get_available_actions("nonexistent")


# ---------------------------------------------------------------------------
# get_state_label tests
# ---------------------------------------------------------------------------

class TestGetStateLabel:
    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def _make_task(self, task_id, status="ready", reason=None):
        task = await self.db.create_task(
            id=task_id, project_id=PROJECT_ID, goal="test",
        )
        updates = {}
        if status != "ready":
            updates["status"] = status
        if reason is not None:
            updates["reason"] = reason
        if updates:
            task = await self.db.update_task(task_id, **updates)
        return task


    async def test_stopped_unknown_reason_falls_back(self):
        await self._make_task("t/lbl9", status="stopped", reason="some_new_reason")
        label = await self.lifecycle.get_state_label("t/lbl9")
        # Falls back to (stopped, None) label
        assert label["state"] == "stopped"
        assert label["label"] == "Stopped"
        assert label["reason"] == "some_new_reason"


    async def test_task_not_found_raises(self):
        with pytest.raises(ValueError, match="not found"):
            await self.lifecycle.get_state_label("nonexistent")

    async def test_all_state_labels_have_required_keys(self):
        """Every entry in STATE_LABELS has label, color, and pulse."""
        for key, info in STATE_LABELS.items():
            assert "label" in info, f"Missing 'label' for {key}"
            assert "color" in info, f"Missing 'color' for {key}"
            assert "pulse" in info, f"Missing 'pulse' for {key}"


# ---------------------------------------------------------------------------
# Transition table completeness
# ---------------------------------------------------------------------------

class TestTransitionTableCompleteness:
    def test_all_transitions_have_to_state(self):
        for key, tdef in TRANSITIONS.items():
            assert tdef.to_state is not None, f"Missing to_state for {key}"

    def test_transition_count(self):
        """Verify we have the expected number of transitions from the design."""
        # 16 user + 9 system + 7 recovery = 32, plus ("ready", "approve") = 37
        assert len(TRANSITIONS) == 37

    def test_all_user_actions_have_labels(self):
        """User-facing actions should have labels for dashboard buttons."""
        user_actions = [
            ("ready", "dispatch"), ("ready", "approve"), ("ready", "cancel"),
            ("working", "stop"),
            # ("working", "cancel") — intentionally no label; not shown in dashboard
            ("validating", "stop"), ("validating", "skip_gate"),
            # ("validating", "cancel") — intentionally no label; not shown in dashboard
            ("stopped", "resume"), ("stopped", "retry"), ("stopped", "start"),
            ("stopped", "skip_gate"), ("stopped", "cancel"), ("stopped", "close"),
            ("completed", "reopen"),
            ("cancelled", "retry"), ("cancelled", "resume"),
        ]
        for key in user_actions:
            assert TRANSITIONS[key].label, f"Missing label for user action {key}"

    def test_working_cancel_and_validating_cancel_have_no_label(self):
        """Cancel for working/validating has no label — not shown in dashboard UI.

        User flow: Stop first (lands in stopped), then Cancel from stopped.
        Transitions still exist for MCP tools and programmatic use.
        """
        assert not TRANSITIONS[("working", "cancel")].label
        assert not TRANSITIONS[("validating", "cancel")].label

    def test_status_map_covers_all_old_values(self):
        expected = {
            "pending-validation", "needs-review", "turns-exhausted",
            "rate-limited", "failed", "reopened", "merged", "blocked",
            "ready", "working", "validating", "stopped", "completed", "cancelled",
        }
        assert set(_STATUS_MAP.keys()) == expected

    def test_state_fallbacks_cover_all_states(self):
        expected = {"ready", "working", "validating", "stopped", "completed", "cancelled"}
        assert set(_STATE_FALLBACKS.keys()) == expected


# ---------------------------------------------------------------------------
# Service importability
# ---------------------------------------------------------------------------

class TestImportability:
    def test_lifecycle_module_imports(self):
        from switchboard.dispatch import lifecycle
        assert hasattr(lifecycle, "TaskLifecycle")
        assert hasattr(lifecycle, "TRANSITIONS")
        assert hasattr(lifecycle, "STATE_LABELS")
        assert hasattr(lifecycle, "IllegalTransition")
        assert hasattr(lifecycle, "TransitionDef")


    def test_singleton_exists(self):
        from switchboard.dispatch.lifecycle import lifecycle
        assert isinstance(lifecycle, TaskLifecycle)


# ---------------------------------------------------------------------------
# Behavior tests — cancel / close / skip_gate through lifecycle.execute()
# ---------------------------------------------------------------------------

class TestCancelBehavior:
    """Test cancel transition through lifecycle with real DB."""

    @pytest.fixture(autouse=True)
    async def setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        try:
            await db.create_project(
                id=PROJECT_ID,
                repo="https://github.com/test/repo.git",
                working_dir="/tmp/lifecycle-test",
            )
        except Exception:
            pass


class TestCloseBehavior:
    """Test close transition through lifecycle with real DB."""

    @pytest.fixture(autouse=True)
    async def setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        try:
            await db.create_project(
                id=PROJECT_ID,
                repo="https://github.com/test/repo.git",
                working_dir="/tmp/lifecycle-test",
            )
        except Exception:
            pass


class TestSkipGateBehavior:
    """Test skip_gate transition through lifecycle with real DB."""

    @pytest.fixture(autouse=True)
    async def setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        try:
            await db.create_project(
                id=PROJECT_ID,
                repo="https://github.com/test/repo.git",
                working_dir="/tmp/lifecycle-test",
            )
        except Exception:
            pass


    async def test_skip_gate_posts_message(self):
        from switchboard.dispatch.lifecycle import TRANSITIONS
        from unittest.mock import AsyncMock
        tdef = TRANSITIONS[("validating", "skip_gate")]
        orig = tdef.side_effects[:]
        # Keep all effects except dispatch_dependents (last one) — mock that out
        tdef.side_effects = orig[:-1] + [AsyncMock()]
        try:
            await _seed(self.db, status="pending-validation")
            await self.lifecycle.execute(TASK_ID, "skip_gate")
            result = await self.db.read_task_messages(TASK_ID)
            messages = result["messages"]
            gate_msgs = [m for m in messages if m.get("title") == "Gate skipped"]
            assert len(gate_msgs) == 1
        finally:
            tdef.side_effects = orig


class TestCancelChainBehavior:
    """Test cancel_chain routes through lifecycle."""

    @pytest.fixture(autouse=True)
    async def setup(self, db, mock_git, mock_sdk):
        self.db = db
        try:
            await db.create_project(
                id="chain-proj",
                repo="https://github.com/test/repo.git",
                working_dir="/tmp/chain-test",
            )
        except Exception:
            pass
        await db.create_task(
            id="chain-proj/root", project_id="chain-proj", goal="root",
        )
        await db.update_task("chain-proj/root", status="working")
        await db.create_task(
            id="chain-proj/child", project_id="chain-proj", goal="child",
            depends_on="chain-proj/root",
        )
        await db.create_task(
            id="chain-proj/grandchild", project_id="chain-proj", goal="grandchild",
            depends_on="chain-proj/child",
        )


    async def test_cancel_chain_skips_completed(self):
        from switchboard.dispatch.engine import cancel_chain
        await self.db.update_task("chain-proj/child", status="completed")
        result = await cancel_chain("chain-proj/root")
        assert "chain-proj/child" not in result["cancelled"]
        # grandchild should still be cancelled (it's ready)
        assert "chain-proj/grandchild" in result["cancelled"]


# ---------------------------------------------------------------------------
# Stop behavior tests
# ---------------------------------------------------------------------------


class TestStopBehavior:
    """Test stop_task transition through lifecycle with real DB."""

    @pytest.fixture(autouse=True)
    async def setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        try:
            await db.create_project(
                id=PROJECT_ID,
                repo="https://github.com/test/repo.git",
                working_dir="/tmp/lifecycle-test",
            )
        except Exception:
            pass


    async def test_resume_after_stop(self):
        """After stop, resume should work and preserve session_id."""
        await _seed(self.db, status="working")
        await self.db.update_task(TASK_ID, session_id="sess-456")
        await self.lifecycle.execute(TASK_ID, "stop")
        # Now resume
        result = await self.lifecycle.execute(TASK_ID, "resume")
        assert result["status"] == "working"
        task = await self.db.get_task(TASK_ID)
        assert task["session_id"] == "sess-456"


    async def test_stop_validating_cancels_gate_task(self):
        """Stop from validating should cancel the gate asyncio task via _gate_tasks."""
        from switchboard.dispatch._state import _gate_tasks
        await _seed(self.db, status="pending-validation", gate_status="testing")
        # Create a mock gate asyncio task
        mock_task = asyncio.Future()
        _gate_tasks[TASK_ID] = mock_task
        try:
            await self.lifecycle.execute(TASK_ID, "stop")
            assert mock_task.cancelled()
            assert TASK_ID not in _gate_tasks
        finally:
            _gate_tasks.pop(TASK_ID, None)


# ---------------------------------------------------------------------------
# Reason-aware action filtering tests
# ---------------------------------------------------------------------------

class TestActionsFiltered:
    """Test get_available_actions for each (state, reason) pair in the button matrix."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id="filt-proj",
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def _make(self, task_id, status="ready", reason=None, **kwargs):
        task = await self.db.create_task(id=task_id, project_id="filt-proj", goal="test")
        updates = {}
        if status != "ready":
            updates["status"] = status
        if reason is not None:
            updates["reason"] = reason
        if kwargs:
            updates.update(kwargs)
        if updates:
            task = await self.db.update_task(task_id, **updates)
        return task

    async def _names(self, task_id):
        actions = await self.lifecycle.get_available_actions(task_id)
        return {a["name"] for a in actions}

    # ready sub-states

    async def test_ready_held(self):
        await self._make("f/r2", held=True)
        names = await self._names("f/r2")
        assert names == {"approve", "cancel"}


    async def test_ready_blocked(self):
        await self._make("f/r4", status="blocked")
        names = await self._names("f/r4")
        assert names == {"cancel"}

    # working

    # validating

    # stopped — paused_by_user / turns_exhausted / wall_clock_timeout / rate_limited


    # stopped — gate failure reasons → skip_gate appears


    # stopped — dispatch_error / push_failed / worktree_missing → no skip_gate


    # stopped — awaiting_feedback → start, cancel_reopen (no close)

    # stopped — recovery_limit → retry, end_task (no skip_gate)

    # completed

    # cancelled


    # Precondition filtering: resume without session filtered

    # Confirm flags — resume/retry on stopped must have confirm=False (no-confirm path)

    # Compound end_task structure

    # System actions never appear


# ---------------------------------------------------------------------------
# get_state_label: ready sub-state reason derivation
# ---------------------------------------------------------------------------

class TestGetStateLabelReadySubstates:
    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id="lbl-proj2",
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lifecycle-test",
        )

    async def test_ready_held_label(self):
        task = await self.db.create_task(id="lbl-proj2/h1", project_id="lbl-proj2", goal="test")
        await self.db.update_task("lbl-proj2/h1", held=True)
        label = await self.lifecycle.get_state_label("lbl-proj2/h1")
        assert label["label"] == "Held"

    async def test_ready_blocked_label(self):
        task = await self.db.create_task(id="lbl-proj2/b1", project_id="lbl-proj2", goal="test")
        await self.db.update_task("lbl-proj2/b1", status="blocked")
        label = await self.lifecycle.get_state_label("lbl-proj2/b1")
        assert label["label"] == "Blocked"


# ---------------------------------------------------------------------------
# Dashboard API: GET /dashboard/api/tasks/{id}/actions endpoint
# ---------------------------------------------------------------------------

import json as _json


def _make_api_scope(path: str, method: str = "GET") -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "session_user": {"id": 1, "email": "owner@localhost", "name": "Owner", "role": "owner"},
    }


def _make_api_receive():
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return receive


class _ApiCapture:
    def __init__(self):
        self.status = None
        self.body = b""

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")

    def json(self):
        return _json.loads(self.body)


class TestDashboardActionsEndpoint:
    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        await db.create_project(
            id="api-act-proj",
            repo="https://github.com/test/repo.git",
            working_dir="/tmp",
        )
        self.task = await db.create_task(
            id="api-act-proj/t1",
            project_id="api-act-proj",
            goal="test actions endpoint",
        )

    async def test_actions_200_shape(self):
        from switchboard.dashboard.api import handle_request
        scope = _make_api_scope("/dashboard/api/tasks/api-act-proj%2Ft1/actions")
        resp = _ApiCapture()
        await handle_request(scope, _make_api_receive(), resp)
        assert resp.status == 200
        data = resp.json()
        assert data["task_id"] == "api-act-proj/t1"
        assert "state" in data
        assert "actions" in data
        state = data["state"]
        assert "status" in state
        assert "label" in state
        assert "color" in state
        assert "pulse" in state
        # ready task should have dispatch and cancel actions (hyphenated names)
        action_names = {a["name"] for a in data["actions"]}
        assert "dispatch" in action_names
        assert "cancel" in action_names
        # Each action has required fields
        for action in data["actions"]:
            assert "name" in action
            assert "label" in action
            assert "style" in action
            assert "confirm" in action

    async def test_actions_404_nonexistent(self):
        from switchboard.dashboard.api import handle_request
        scope = _make_api_scope("/dashboard/api/tasks/nonexistent%2Ftask/actions")
        resp = _ApiCapture()
        await handle_request(scope, _make_api_receive(), resp)
        assert resp.status == 404

    async def test_actions_hyphenated_names(self):
        """Action names returned by endpoint use hyphens, not underscores."""
        from switchboard.dashboard.api import handle_request
        # Set task to stopped with awaiting_feedback to get cancel_reopen action
        await self.db.update_task("api-act-proj/t1", status="stopped", reason="awaiting_feedback")
        scope = _make_api_scope("/dashboard/api/tasks/api-act-proj%2Ft1/actions")
        resp = _ApiCapture()
        await handle_request(scope, _make_api_receive(), resp)
        assert resp.status == 200
        data = resp.json()
        names = {a["name"] for a in data["actions"]}
        assert "cancel-reopen" in names  # cancel_reopen → cancel-reopen
        assert "cancel_reopen" not in names  # underscore version should NOT appear


# ---------------------------------------------------------------------------
# Per-task locking tests
# ---------------------------------------------------------------------------


class TestLifecycleLocking:
    """Tests for the per-task asyncio.Lock on lifecycle.execute()."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id="lock-proj",
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/lock-test",
        )

    async def _make_task(self, task_id, status="ready", **extra):
        await self.db.create_task(id=task_id, project_id="lock-proj", goal="test")
        updates = {}
        if status != "ready":
            updates["status"] = status
        updates.update(extra)
        if updates:
            await self.db.update_task(task_id, **updates)


# ---------------------------------------------------------------------------
# Transition table inspection tests
# ---------------------------------------------------------------------------


class TestTransitionTableFixes:
    """Verify the transition table has the expected side effects."""

    def test_recover_park_includes_stop_cc_session(self):
        """recover_park from working state must stop the CC session."""
        from switchboard.dispatch.lifecycle import _stop_cc_session
        tdef = TRANSITIONS[("working", "recover_park")]
        assert _stop_cc_session in tdef.side_effects, (
            "recover_park must include _stop_cc_session to kill the running session"
        )

    def test_recover_park_stop_session_is_first(self):
        """_stop_cc_session should run before other side effects in recover_park."""
        from switchboard.dispatch.lifecycle import _stop_cc_session
        tdef = TRANSITIONS[("working", "recover_park")]
        assert tdef.side_effects[0] is _stop_cc_session, (
            "_stop_cc_session should be the first side effect in recover_park"
        )


# ---------------------------------------------------------------------------
# last_activity update tests
# ---------------------------------------------------------------------------


class TestLastActivityOnSessionLaunch:
    """Verify that session-launching side effects update last_activity."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db, mock_git, mock_sdk):
        self.db = db
        self.lifecycle = TaskLifecycle()
        await db.create_project(
            id="la-proj",
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/la-test",
        )


