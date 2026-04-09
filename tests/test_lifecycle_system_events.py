"""Behavior tests for system-initiated events through lifecycle.execute().

Tests SDK session outcomes (complete, exhaust_turns, timeout, rate_limit, error,
signal_kill) and gate outcomes (gate_pass, gate_fail) routed through lifecycle.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import switchboard.db as db
from switchboard.dispatch.lifecycle import (
    IllegalTransition,
    TaskLifecycle,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

PROJECT_ID = "sys-event-proj"
TASK_PREFIX = "sys-event-proj"


async def _seed(db_mod, task_suffix, status="working", **extra):
    """Create project + task at given status."""
    try:
        await db_mod.create_project(
            id=PROJECT_ID,
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/sys-event-test",
            test_command="pytest tests/",
        )
    except Exception:
        pass
    task_id = f"{TASK_PREFIX}/{task_suffix}"
    await db_mod.create_task(id=task_id, project_id=PROJECT_ID, goal="test system event")
    if status != "ready" or extra:
        await db_mod.update_task(task_id, status=status, **extra)
    return task_id


def _mock_result_msg(**overrides):
    """Create a mock ResultMessage."""
    msg = MagicMock()
    msg.num_turns = overrides.get("num_turns", 10)
    msg.duration_ms = overrides.get("duration_ms", 60000)
    msg.total_cost_usd = overrides.get("total_cost_usd", 1.5)
    msg.result = overrides.get("result", "Task completed successfully.")
    msg.stop_reason = overrides.get("stop_reason", "end_turn")
    msg.is_error = overrides.get("is_error", False)
    msg.session_id = overrides.get("session_id", "sess-123")
    return msg


def _system_event_patches():
    """Patches to suppress real side-effect operations in system event handlers."""
    return [
        patch("switchboard.git.operations._ensure_branch_pushed", AsyncMock(return_value=True)),
        patch("switchboard.dispatch.gates._run_test_gate", AsyncMock()),
        patch("switchboard.dispatch.gates._dispatch_review", AsyncMock()),
        patch("switchboard.dispatch.gates._process_review_result", AsyncMock()),
        patch("switchboard.dispatch.engine._update_usage", AsyncMock()),
        patch("switchboard.dispatch.engine._check_and_dispatch_dependents", AsyncMock()),
        patch("switchboard.dispatch.queue._drain_queue", AsyncMock()),
        patch("switchboard.notifications.slack.task_completed", AsyncMock()),
        patch("switchboard.notifications.slack.task_failed", AsyncMock()),
        patch("switchboard.notifications.slack.task_needs_review", AsyncMock()),
        patch("switchboard.db.resolve_punchlist_items_for_task", AsyncMock(return_value=0)),
    ]


# ---------------------------------------------------------------------------
# SDK Complete
# ---------------------------------------------------------------------------


class TestCompleteEvent:
    """(working, complete) → validating"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()


    async def test_complete_calls_push_and_gate(self):
        task_id = await _seed(self.db_mod, "complete-gate",
                              auto_test=True)
        result_msg = _mock_result_msg()

        push_mock = AsyncMock(return_value=True)
        gate_mock = AsyncMock()
        usage_mock = AsyncMock()

        patches = _system_event_patches()
        for p in patches:
            p.start()

        # Override specific mocks
        with patch("switchboard.git.operations._ensure_branch_pushed", push_mock), \
             patch("switchboard.dispatch.gates._run_test_gate", gate_mock), \
             patch("switchboard.dispatch.engine._update_usage", usage_mock):
            await self.lifecycle.execute(task_id, "complete",
                triggered_by="system", result_msg=result_msg)

        for p in patches:
            p.stop()

        push_mock.assert_called_once()
        gate_mock.assert_called_once()
        usage_mock.assert_called_once()


# ---------------------------------------------------------------------------
# SDK Exhaust Turns
# ---------------------------------------------------------------------------


class TestExhaustTurnsEvent:
    """(working, exhaust_turns) → always stopped/turns_exhausted"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()

    async def test_exhaust_turns_with_gates(self):
        """Even with test_command configured, turns exhausted always stops — work is incomplete."""
        task_id = await _seed(self.db_mod, "exhaust-gates",
                              auto_test=True)
        result_msg = _mock_result_msg(stop_reason="max_turns")

        project = await self.db_mod.get_project(PROJECT_ID)

        patches = _system_event_patches()
        for p in patches:
            p.start()
        try:
            task = await self.lifecycle.execute(task_id, "exhaust_turns",
                triggered_by="system", result_msg=result_msg, project=project)
        finally:
            for p in patches:
                p.stop()

        assert task["status"] == "stopped"
        assert task["reason"] == "turns_exhausted"


# ---------------------------------------------------------------------------
# SDK Timeout
# ---------------------------------------------------------------------------


class TestTimeoutEvent:
    """(working, timeout) → stopped(wall_clock_timeout)"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()


    async def test_timeout_posts_message(self):
        task_id = await _seed(self.db_mod, "timeout-msg")

        patches = _system_event_patches()
        for p in patches:
            p.start()
        try:
            await self.lifecycle.execute(task_id, "timeout",
                triggered_by="system", max_wall_clock_minutes=60)
        finally:
            for p in patches:
                p.stop()

        msgs = await self.db_mod.read_task_messages(task_id)
        titles = [m["title"] for m in msgs.get("messages", []) if m.get("title")]
        assert any("Wall clock timeout" in t for t in titles)


# ---------------------------------------------------------------------------
# SDK Rate Limit
# ---------------------------------------------------------------------------


class TestRateLimitEvent:
    """(working, rate_limit) → stopped(rate_limited)"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()


    async def test_rate_limit_sets_retry_after(self):
        task_id = await _seed(self.db_mod, "ratelimit-retry")
        result_msg = _mock_result_msg(is_error=True, result="hit your limit")

        patches = _system_event_patches()
        for p in patches:
            p.start()
        try:
            await self.lifecycle.execute(task_id, "rate_limit",
                triggered_by="system", result_msg=result_msg,
                retry_after="2026-04-02T05:05:00Z")
        finally:
            for p in patches:
                p.stop()

        task = await self.db_mod.get_task(task_id)
        assert task["retry_after"] == "2026-04-02T05:05:00Z"


# ---------------------------------------------------------------------------
# SDK Error
# ---------------------------------------------------------------------------


class TestErrorEvent:
    """(working, error) → stopped(dispatch_error)"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()

    async def test_error_stops_task(self):
        task_id = await _seed(self.db_mod, "error-basic")
        result_msg = _mock_result_msg(is_error=True, stop_reason="error",
                                       result="Something went wrong")

        patches = _system_event_patches()
        for p in patches:
            p.start()
        try:
            task = await self.lifecycle.execute(task_id, "error",
                triggered_by="system", result_msg=result_msg)
        finally:
            for p in patches:
                p.stop()

        assert task["status"] == "stopped"
        assert task["reason"] == "dispatch_error"


    async def test_error_no_result_no_message(self):
        """Error with no result_msg and no error_message posts generic message."""
        task_id = await _seed(self.db_mod, "error-no-result")

        patches = _system_event_patches()
        for p in patches:
            p.start()
        try:
            task = await self.lifecycle.execute(task_id, "error",
                triggered_by="system")
        finally:
            for p in patches:
                p.stop()

        assert task["status"] == "stopped"
        msgs = await self.db_mod.read_task_messages(task_id)
        titles = [m["title"] for m in msgs.get("messages", []) if m.get("title")]
        assert any("Session ended without result" in t for t in titles)


# ---------------------------------------------------------------------------
# Signal Kill
# ---------------------------------------------------------------------------


class TestSignalKillEvent:
    """(working, signal_kill) → working (stays working, recovery_priority set)"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()


    async def test_signal_kill_posts_message(self):
        task_id = await _seed(self.db_mod, "sigkill-msg")

        await self.lifecycle.execute(task_id, "signal_kill",
            triggered_by="system", error_message="exit code -15")

        msgs = await self.db_mod.read_task_messages(task_id)
        titles = [m["title"] for m in msgs.get("messages", []) if m.get("title")]
        assert any("Session killed by signal" in t for t in titles)


# ---------------------------------------------------------------------------
# Gate Pass
# ---------------------------------------------------------------------------


class TestGatePassEvent:
    """(validating, gate_pass) → completed(gate_passed)"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()


    async def test_gate_pass_resolves_punchlist(self):
        task_id = await _seed(self.db_mod, "gatepass-punchlist",
                              status="validating")

        resolve_mock = AsyncMock(return_value=2)
        patches = _system_event_patches()
        for p in patches:
            p.start()

        with patch("switchboard.db.resolve_punchlist_items_for_task", resolve_mock):
            await self.lifecycle.execute(task_id, "gate_pass",
                triggered_by="gate-pipeline")

        for p in patches:
            p.stop()

        resolve_mock.assert_called_once_with(task_id)


# ---------------------------------------------------------------------------
# Gate Fail
# ---------------------------------------------------------------------------


class TestGateFailEvent:
    """(validating, gate_fail) → stopped(reason)"""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db_mod = db
        self.lifecycle = TaskLifecycle()


    async def test_gate_fail_max_review_retries(self):
        task_id = await _seed(self.db_mod, "gatefail-review",
                              status="validating")

        patches = _system_event_patches()
        for p in patches:
            p.start()
        try:
            task = await self.lifecycle.execute(task_id, "gate_fail",
                triggered_by="gate-pipeline", reason="max_review_retries")
        finally:
            for p in patches:
                p.stop()

        assert task["status"] == "stopped"
        assert task["reason"] == "max_review_retries"


# ---------------------------------------------------------------------------
# Verification: no direct status updates in migrated modules
# ---------------------------------------------------------------------------


class TestNoDirectStatusUpdates:
    """Verify migrated modules don't call db.update_task(status=...) directly."""

    def test_sdk_session_no_status_updates(self):
        """sdk_session.py should not set task status directly."""
        import inspect
        from switchboard.dispatch import sdk_session
        source = inspect.getsource(sdk_session)
        # Find db.update_task calls with status= parameter
        import re
        matches = re.findall(r'db\.update_task\([^)]*\bstatus\s*=', source)
        assert len(matches) == 0, f"Found {len(matches)} direct status updates in sdk_session.py"

    def test_gates_no_task_status_updates(self):
        """gates.py should not set task-level status directly (gate_status is OK)."""
        import inspect
        from switchboard.dispatch import gates
        source = inspect.getsource(gates)
        import re
        # Match db.update_task(...status=...) but exclude gate_status= and pr_status=
        # Find all db.update_task calls
        calls = re.findall(r'db\.update_task\([^)]+\)', source)
        for call in calls:
            # Check if it has status= that isn't gate_status= or pr_status=
            if re.search(r'(?<!gate_)(?<!pr_)\bstatus\s*=', call):
                pytest.fail(f"Found direct status update in gates.py: {call}")

    def test_engine_check_dispatch_dependents_no_status(self):
        """_check_and_dispatch_dependents should not set status directly."""
        import inspect
        from switchboard.dispatch.engine import _check_and_dispatch_dependents
        source = inspect.getsource(_check_and_dispatch_dependents)
        import re
        matches = re.findall(r'db\.update_task\([^)]*\bstatus\s*=', source)
        assert len(matches) == 0, f"Found {len(matches)} direct status updates in _check_and_dispatch_dependents"
