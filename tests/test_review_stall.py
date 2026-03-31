"""Tests for review subtask stall detection and recovery.

Covers:
- _read_last_jsonl_timestamp utility
- REVIEW_INACTIVITY_TIMEOUT_SECONDS constant
- _run_subtask inactivity watchdog (stall detection)
- _dispatch_review strike 1 (resume same session)
- _dispatch_review strike 2 (halt, set needs-review)
- retry_task re-runs gate pipeline for completed + gate_status=needs-review
"""

import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# _read_last_jsonl_timestamp — pure utility
# ---------------------------------------------------------------------------

class TestReadLastJsonlTimestamp:
    """_read_last_jsonl_timestamp reads last timestamp from session JSONL."""

    def setup_method(self):
        from switchboard.dispatch.gates import _read_last_jsonl_timestamp
        self.fn = _read_last_jsonl_timestamp

    def test_nonexistent_file_returns_none(self, tmp_path):
        assert self.fn(tmp_path / "missing.jsonl") is None

    def test_empty_file_returns_none(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        assert self.fn(p) is None

    def test_single_entry_returns_its_timestamp(self, tmp_path):
        ts = "2026-03-31T12:00:00Z"
        p = tmp_path / "session.jsonl"
        p.write_text(json.dumps({"timestamp": ts, "type": "AssistantMessage"}) + "\n")
        result = self.fn(p)
        assert result is not None
        assert result == datetime(2026, 3, 31, 12, 0, 0, tzinfo=timezone.utc)

    def test_returns_last_timestamp_when_multiple_entries(self, tmp_path):
        p = tmp_path / "session.jsonl"
        entries = [
            {"timestamp": "2026-03-31T10:00:00Z", "type": "UserMessage"},
            {"timestamp": "2026-03-31T11:00:00Z", "type": "AssistantMessage"},
            {"timestamp": "2026-03-31T12:00:00Z", "type": "UserMessage"},
        ]
        p.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        result = self.fn(p)
        assert result == datetime(2026, 3, 31, 12, 0, 0, tzinfo=timezone.utc)

    def test_skips_blank_lines(self, tmp_path):
        p = tmp_path / "session.jsonl"
        p.write_text(
            json.dumps({"timestamp": "2026-03-31T09:00:00Z", "type": "UserMessage"}) + "\n"
            "\n"
            "\n"
        )
        result = self.fn(p)
        assert result is not None

    def test_skips_malformed_lines(self, tmp_path):
        p = tmp_path / "session.jsonl"
        p.write_text(
            json.dumps({"timestamp": "2026-03-31T09:00:00Z", "type": "UserMessage"}) + "\n"
            "not-json\n"
        )
        # Should fall back to the valid entry
        result = self.fn(p)
        assert result is not None
        assert result == datetime(2026, 3, 31, 9, 0, 0, tzinfo=timezone.utc)

    def test_entry_without_timestamp_field_skipped(self, tmp_path):
        p = tmp_path / "session.jsonl"
        p.write_text(
            json.dumps({"timestamp": "2026-03-31T08:00:00Z", "type": "UserMessage"}) + "\n"
            + json.dumps({"type": "AssistantMessage", "content": []}) + "\n"  # no timestamp
        )
        result = self.fn(p)
        # Should find the one with a timestamp
        assert result == datetime(2026, 3, 31, 8, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# REVIEW_INACTIVITY_TIMEOUT_SECONDS constant
# ---------------------------------------------------------------------------

class TestReviewInactivityConstant:
    def test_constant_exists_and_is_300(self):
        from switchboard.config.constants import REVIEW_INACTIVITY_TIMEOUT_SECONDS
        assert REVIEW_INACTIVITY_TIMEOUT_SECONDS == 300

    def test_constant_is_reasonable(self):
        from switchboard.config.constants import REVIEW_INACTIVITY_TIMEOUT_SECONDS
        # Must be positive and at least a minute
        assert REVIEW_INACTIVITY_TIMEOUT_SECONDS >= 60


# ---------------------------------------------------------------------------
# _run_subtask inactivity watchdog
# ---------------------------------------------------------------------------

def _make_hanging_sdk_client():
    """Return a mock ClaudeSDKClient that hangs indefinitely without writing to JSONL."""

    async def _hang_generator():
        """Async generator that hangs forever (simulates stalled reviewer)."""
        await asyncio.sleep(60)  # hang much longer than any test timeout
        return
        yield  # make it an async generator

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.query = AsyncMock()
    mock_client.receive_response = MagicMock(return_value=_hang_generator())
    return mock_client


def _make_completing_sdk_client(session_id="sess_abc123"):
    """Return a mock ClaudeSDKClient that immediately sends one AssistantMessage + ResultMessage."""
    from claude_agent_sdk import AssistantMessage, ResultMessage

    async def _fast_generator():
        # Emit an AssistantMessage so session_id gets captured
        assistant_msg = MagicMock(spec=AssistantMessage)
        assistant_msg.session_id = session_id
        assistant_msg.content = []
        yield assistant_msg
        # Emit a ResultMessage to complete
        result_msg = MagicMock(spec=ResultMessage)
        result_msg.is_error = False
        result_msg.result = "Task completed."
        result_msg.usage = {"input_tokens": 100, "output_tokens": 50,
                            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        result_msg.total_cost_usd = 0.001
        result_msg.duration_ms = 5000
        yield result_msg

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.query = AsyncMock()
    mock_client.receive_response = MagicMock(return_value=_fast_generator())
    return mock_client


class TestRunSubtaskInactivityWatchdog:
    """_run_subtask inactivity watchdog: stall detection and session_id capture."""

    @pytest.fixture(autouse=True)
    def _setup_worktree(self, tmp_path):
        self.worktree = tmp_path / "worktree"
        self.worktree.mkdir()
        (self.worktree / ".switchboard").mkdir()

    @pytest.fixture(autouse=True)
    def _patch_worker_user(self):
        """Patch pwd.getpwnam so tests don't need a real 'switchboard' user."""
        import pwd
        mock_pw = MagicMock()
        mock_pw.pw_dir = str(Path.home())
        with patch("switchboard.dispatch.gates.pwd.getpwnam", return_value=mock_pw):
            yield

    async def _make_task(self, db, sample_project):
        task = await db.create_task(
            id="test-project/stall-task",
            project_id="test-project",
            goal="Test stall detection",
        )
        return await db.update_task(task["id"], status="working",
                                    worktree_path=str(self.worktree))

    async def test_stall_detected_returns_stalled_status(self, db, sample_project):
        """When SDK hangs and JSONL is empty, watchdog triggers and subtask is stalled."""
        await self._make_task(db, sample_project)

        mock_update_usage = AsyncMock()
        hanging_client = _make_hanging_sdk_client()

        with patch("switchboard.dispatch.gates.ClaudeSDKClient", return_value=hanging_client), \
             patch("switchboard.dispatch.engine._update_usage", mock_update_usage):
            from switchboard.dispatch.gates import _run_subtask
            subtask = await _run_subtask(
                task_id="test-project/stall-task",
                subtask_type="review",
                prompt="review this",
                inactivity_timeout=1,  # 1 second — fast for tests
            )

        assert subtask["status"] == "stalled"

    async def test_stall_returns_captured_session_id(self, db, sample_project):
        """Stalled subtask includes _captured_session_id for strike 1 resume."""
        await self._make_task(db, sample_project)

        # SDK writes one AssistantMessage with session_id, then hangs
        from claude_agent_sdk import AssistantMessage

        async def _write_then_hang():
            msg = MagicMock(spec=AssistantMessage)
            msg.session_id = "ses_captured_abc"
            msg.content = []
            yield msg
            await asyncio.sleep(60)  # hang after first message

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.query = AsyncMock()
        mock_client.receive_response = MagicMock(return_value=_write_then_hang())

        # Patch _read_last_jsonl_timestamp to fast-forward: return a stale timestamp
        stale_ts = datetime.now(timezone.utc) - timedelta(seconds=10)
        mock_update_usage = AsyncMock()

        with patch("switchboard.dispatch.gates.ClaudeSDKClient", return_value=mock_client), \
             patch("switchboard.dispatch.gates._read_last_jsonl_timestamp", return_value=stale_ts), \
             patch("switchboard.dispatch.engine._update_usage", mock_update_usage):
            from switchboard.dispatch.gates import _run_subtask
            subtask = await _run_subtask(
                task_id="test-project/stall-task",
                subtask_type="review",
                prompt="review this",
                inactivity_timeout=1,
            )

        assert subtask["status"] == "stalled"
        assert subtask["_captured_session_id"] == "ses_captured_abc"

    async def test_no_stall_on_active_session(self, db, sample_project):
        """SDK that completes normally does not trigger stall."""
        await self._make_task(db, sample_project)

        mock_update_usage = AsyncMock()
        completing_client = _make_completing_sdk_client()

        with patch("switchboard.dispatch.gates.ClaudeSDKClient", return_value=completing_client), \
             patch("switchboard.dispatch.engine._update_usage", mock_update_usage):
            from switchboard.dispatch.gates import _run_subtask
            subtask = await _run_subtask(
                task_id="test-project/stall-task",
                subtask_type="review",
                prompt="review this",
                inactivity_timeout=30,  # 30 second timeout — won't fire before completion
            )

        assert subtask["status"] == "completed"

    async def test_resume_session_id_sets_options_resume(self, db, sample_project):
        """resume_session_id parameter is passed to options.resume."""
        await self._make_task(db, sample_project)

        mock_update_usage = AsyncMock()
        completing_client = _make_completing_sdk_client()
        captured_options = []

        original_init = MagicMock(return_value=completing_client)

        def capture_options(options=None, **kwargs):
            captured_options.append(options)
            return completing_client

        with patch("switchboard.dispatch.gates.ClaudeSDKClient", side_effect=capture_options), \
             patch("switchboard.dispatch.engine._update_usage", mock_update_usage):
            from importlib import reload
            import switchboard.dispatch.gates as gates_mod
            # Reload to get fresh import
            subtask = await gates_mod._run_subtask(
                task_id="test-project/stall-task",
                subtask_type="review",
                prompt="review this",
                resume_session_id="ses_resume_xyz",
            )

        assert len(captured_options) == 1
        assert hasattr(captured_options[0], "resume")
        assert captured_options[0].resume == "ses_resume_xyz"

    async def test_stalled_subtask_db_record_has_stalled_status(self, db, sample_project):
        """DB record for stalled subtask has status='stalled'."""
        await self._make_task(db, sample_project)

        mock_update_usage = AsyncMock()
        # Use stale timestamp to fast-track the watchdog
        stale_ts = datetime.now(timezone.utc) - timedelta(seconds=100)

        with patch("switchboard.dispatch.gates.ClaudeSDKClient", return_value=_make_hanging_sdk_client()), \
             patch("switchboard.dispatch.gates._read_last_jsonl_timestamp", return_value=stale_ts), \
             patch("switchboard.dispatch.engine._update_usage", mock_update_usage):
            from switchboard.dispatch.gates import _run_subtask
            subtask = await _run_subtask(
                task_id="test-project/stall-task",
                subtask_type="review",
                prompt="review this",
                inactivity_timeout=1,
            )

        # Verify DB record was updated
        assert subtask["status"] == "stalled"
        db_subtask = await db.get_subtask("test-project/stall-task/review-1")
        assert db_subtask is not None
        assert db_subtask["status"] == "stalled"


# ---------------------------------------------------------------------------
# _dispatch_review strike 1 and strike 2
# ---------------------------------------------------------------------------

class TestDispatchReviewStrikeLogic:
    """_dispatch_review handles stall strikes correctly."""

    @pytest.fixture(autouse=True)
    def _setup_worktree(self, tmp_path):
        self.worktree = tmp_path / "worktree"
        self.worktree.mkdir()

    async def _make_completed_task(self, db, sample_project):
        task = await db.create_task(
            id="test-project/review-task",
            project_id="test-project",
            goal="Test review dispatch",
            auto_review=True,
            review_model="opus",
        )
        return await db.update_task(
            task["id"], status="completed",
            worktree_path=str(self.worktree),
            branch="my-branch",
        )

    async def test_strike1_resumes_with_captured_session_id(self, db, sample_project):
        """Strike 1: when subtask stalls, _dispatch_review resumes with captured session_id."""
        task = await self._make_completed_task(db, sample_project)
        project = await db.get_project("test-project")

        run_subtask_calls = []

        async def mock_run_subtask(task_id, subtask_type, prompt, model="opus",
                                   max_turns=30, resume_session_id=None,
                                   inactivity_timeout=None):
            run_subtask_calls.append({
                "resume_session_id": resume_session_id,
                "inactivity_timeout": inactivity_timeout,
            })
            if len(run_subtask_calls) == 1:
                # First call: stall with captured session_id
                return {"status": "stalled", "_captured_session_id": "ses_stall_abc"}
            # Second call: complete normally
            return {"status": "completed"}

        with patch("switchboard.dispatch.gates._run_subtask", mock_run_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()), \
             patch("switchboard.dispatch.gates._run_as_worker",
                   AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates.notify.task_needs_review", AsyncMock()):
            from switchboard.dispatch.gates import _dispatch_review
            await _dispatch_review("test-project/review-task", project, task)

        assert len(run_subtask_calls) == 2
        # First call: no resume
        assert run_subtask_calls[0]["resume_session_id"] is None
        assert run_subtask_calls[0]["inactivity_timeout"] is not None
        # Second call: resume with captured session_id
        assert run_subtask_calls[1]["resume_session_id"] == "ses_stall_abc"
        assert run_subtask_calls[1]["inactivity_timeout"] is not None

    async def test_strike1_posts_stall_message(self, db, sample_project):
        """Strike 1: a stall message is posted to the task thread."""
        task = await self._make_completed_task(db, sample_project)
        project = await db.get_project("test-project")

        call_count = 0

        async def mock_run_subtask(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"status": "stalled", "_captured_session_id": None}
            return {"status": "completed"}

        with patch("switchboard.dispatch.gates._run_subtask", mock_run_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()), \
             patch("switchboard.dispatch.gates._run_as_worker",
                   AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates.notify.task_needs_review", AsyncMock()):
            from switchboard.dispatch.gates import _dispatch_review
            await _dispatch_review("test-project/review-task", project, task)

        msgs = await db.read_task_messages("test-project/review-task")
        stall_msgs = [m for m in msgs["messages"] if "stalled" in (m.get("title") or "").lower()]
        assert len(stall_msgs) >= 1

    async def test_strike2_sets_gate_status_needs_review(self, db, sample_project):
        """Strike 2: task gate_status set to needs-review when session stalls twice."""
        task = await self._make_completed_task(db, sample_project)
        project = await db.get_project("test-project")

        async def mock_run_subtask(*args, **kwargs):
            # Always stall
            return {"status": "stalled", "_captured_session_id": "ses_abc"}

        mock_notify = AsyncMock()
        with patch("switchboard.dispatch.gates._run_subtask", mock_run_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()), \
             patch("switchboard.dispatch.gates._run_as_worker",
                   AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates.notify.task_needs_review", mock_notify):
            from switchboard.dispatch.gates import _dispatch_review
            await _dispatch_review("test-project/review-task", project, task)

        updated = await db.get_task("test-project/review-task")
        assert updated["gate_status"] == "needs-review"

    async def test_strike2_does_not_call_process_review_result(self, db, sample_project):
        """Strike 2: review result processing is NOT called — gate halts."""
        task = await self._make_completed_task(db, sample_project)
        project = await db.get_project("test-project")

        mock_process = AsyncMock()

        async def mock_run_subtask(*args, **kwargs):
            return {"status": "stalled", "_captured_session_id": None}

        with patch("switchboard.dispatch.gates._run_subtask", mock_run_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", mock_process), \
             patch("switchboard.dispatch.gates._run_as_worker",
                   AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates.notify.task_needs_review", AsyncMock()):
            from switchboard.dispatch.gates import _dispatch_review
            await _dispatch_review("test-project/review-task", project, task)

        mock_process.assert_not_called()

    async def test_strike2_posts_halt_message(self, db, sample_project):
        """Strike 2: a halt message explaining what happened is posted."""
        task = await self._make_completed_task(db, sample_project)
        project = await db.get_project("test-project")

        async def mock_run_subtask(*args, **kwargs):
            return {"status": "stalled", "_captured_session_id": None}

        with patch("switchboard.dispatch.gates._run_subtask", mock_run_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()), \
             patch("switchboard.dispatch.gates._run_as_worker",
                   AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates.notify.task_needs_review", AsyncMock()):
            from switchboard.dispatch.gates import _dispatch_review
            await _dispatch_review("test-project/review-task", project, task)

        msgs = await db.read_task_messages("test-project/review-task")
        halt_msgs = [m for m in msgs["messages"]
                     if "strike 2" in (m.get("title") or "").lower()
                     or "twice" in (m.get("content") or "").lower()]
        assert len(halt_msgs) >= 1

    async def test_strike2_calls_notify(self, db, sample_project):
        """Strike 2: notify.task_needs_review is called."""
        task = await self._make_completed_task(db, sample_project)
        project = await db.get_project("test-project")

        async def mock_run_subtask(*args, **kwargs):
            return {"status": "stalled", "_captured_session_id": None}

        mock_notify = AsyncMock()
        with patch("switchboard.dispatch.gates._run_subtask", mock_run_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()), \
             patch("switchboard.dispatch.gates._run_as_worker",
                   AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates.notify.task_needs_review", mock_notify):
            from switchboard.dispatch.gates import _dispatch_review
            await _dispatch_review("test-project/review-task", project, task)

        mock_notify.assert_called_once()

    async def test_no_stall_on_successful_review(self, db, sample_project):
        """Normal completed subtask: proceeds to _process_review_result_inline."""
        task = await self._make_completed_task(db, sample_project)
        project = await db.get_project("test-project")

        async def mock_run_subtask(*args, **kwargs):
            return {"status": "completed"}

        mock_process = AsyncMock()
        with patch("switchboard.dispatch.gates._run_subtask", mock_run_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", mock_process), \
             patch("switchboard.dispatch.gates._run_as_worker",
                   AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates.notify.task_needs_review", AsyncMock()):
            from switchboard.dispatch.gates import _dispatch_review
            await _dispatch_review("test-project/review-task", project, task)

        mock_process.assert_called_once_with("test-project/review-task")


# ---------------------------------------------------------------------------
# retry_task gate pipeline re-run
# ---------------------------------------------------------------------------

class TestRetryTaskGatePipeline:
    """retry_task re-runs gate pipeline for completed tasks with stalled gate."""

    async def _make_stalled_task(self, db, sample_project, gate_status="needs-review"):
        task = await db.create_task(
            id="test-project/stalled-gate-task",
            project_id="test-project",
            goal="Test gate retry",
        )
        return await db.update_task(
            task["id"],
            status="completed",
            gate_status=gate_status,
            worktree_path="/tmp/fake-worktree",
        )

    async def test_retry_task_reruns_gate_for_completed_needs_review(self, db, sample_project):
        """retry_task with completed + gate_status=needs-review re-runs gate pipeline."""
        task = await self._make_stalled_task(db, sample_project, "needs-review")

        mock_test_gate = AsyncMock()
        with patch("switchboard.dispatch.gates._run_test_gate", mock_test_gate):
            from switchboard.dispatch.engine import retry_task
            result = await retry_task("test-project/stalled-gate-task")

        # Should have scheduled _run_test_gate, not launched a new CC session
        await asyncio.sleep(0)  # let create_task schedule
        mock_test_gate.assert_called_once()
        task_id_called = mock_test_gate.call_args[0][0]
        assert task_id_called == "test-project/stalled-gate-task"

    async def test_retry_task_reruns_gate_for_completed_review_failed(self, db, sample_project):
        """retry_task with completed + gate_status=review-failed re-runs gate pipeline."""
        task = await self._make_stalled_task(db, sample_project, "review-failed")

        mock_test_gate = AsyncMock()
        with patch("switchboard.dispatch.gates._run_test_gate", mock_test_gate):
            from switchboard.dispatch.engine import retry_task
            result = await retry_task("test-project/stalled-gate-task")

        await asyncio.sleep(0)
        mock_test_gate.assert_called_once()

    async def test_retry_task_reruns_gate_for_completed_null_gate_status(self, db, sample_project):
        """retry_task with completed + gate_status=None re-runs gate pipeline."""
        task = await self._make_stalled_task(db, sample_project, None)
        # gate_status=None means gate never ran (or was reset)
        await db.update_task("test-project/stalled-gate-task", gate_status=None)

        mock_test_gate = AsyncMock()
        with patch("switchboard.dispatch.gates._run_test_gate", mock_test_gate):
            from switchboard.dispatch.engine import retry_task
            result = await retry_task("test-project/stalled-gate-task")

        await asyncio.sleep(0)
        mock_test_gate.assert_called_once()

    async def test_retry_task_resets_gate_retries(self, db, sample_project):
        """Gate retries counter is reset when re-running gate pipeline."""
        task = await self._make_stalled_task(db, sample_project, "needs-review")
        await db.update_task("test-project/stalled-gate-task", gate_retries=2)

        mock_test_gate = AsyncMock()
        with patch("switchboard.dispatch.gates._run_test_gate", mock_test_gate):
            from switchboard.dispatch.engine import retry_task
            await retry_task("test-project/stalled-gate-task")

        updated = await db.get_task("test-project/stalled-gate-task")
        assert (updated.get("gate_retries") or 0) == 0

    async def test_retry_task_posts_gate_retry_message(self, db, sample_project):
        """A status message is posted when re-running the gate pipeline."""
        task = await self._make_stalled_task(db, sample_project, "needs-review")

        mock_test_gate = AsyncMock()
        with patch("switchboard.dispatch.gates._run_test_gate", mock_test_gate):
            from switchboard.dispatch.engine import retry_task
            await retry_task("test-project/stalled-gate-task")

        msgs = await db.read_task_messages("test-project/stalled-gate-task")
        gate_msgs = [m for m in msgs["messages"]
                     if "gate" in (m.get("title") or "").lower()
                     or "gate" in (m.get("content") or "").lower()]
        assert len(gate_msgs) >= 1

    async def test_retry_task_normal_behavior_for_non_completed_task(self, db, sample_project,
                                                                       mock_git, tmp_path):
        """retry_task launches a new CC session when task is NOT completed."""
        task = await db.create_task(
            id="test-project/normal-retry",
            project_id="test-project",
            goal="Normal retry test",
        )
        await db.update_task(task["id"], status="needs-review",
                             worktree_path=str(tmp_path))

        mock_test_gate = AsyncMock()
        # Patch dispatch_task to avoid actually dispatching
        with patch("switchboard.dispatch.engine.dispatch_task", AsyncMock(return_value={"status": "working"})), \
             patch("switchboard.dispatch.gates._run_test_gate", mock_test_gate):
            from switchboard.dispatch.engine import retry_task
            await retry_task("test-project/normal-retry")

        # For non-completed task, gate should NOT be re-triggered
        mock_test_gate.assert_not_called()

    async def test_retry_task_does_not_rerun_gate_when_gate_already_passed(self, db, sample_project):
        """retry_task does NOT re-run gate for completed + gate_passed_at set."""
        task = await db.create_task(
            id="test-project/passed-task",
            project_id="test-project",
            goal="Already passed gate",
        )
        await db.update_task(
            task["id"], status="completed",
            gate_status="passed",
            gate_passed_at=db.now_iso(),
            worktree_path="/tmp/fake",
        )

        mock_test_gate = AsyncMock()
        # This should fall through to the normal resume path, not re-run gate
        with patch("switchboard.dispatch.gates._run_test_gate", mock_test_gate), \
             patch("switchboard.dispatch.engine._check_and_dispatch_dependents", AsyncMock()):
            from switchboard.dispatch.engine import retry_task
            # retry_task on a passed completed task should NOT re-run gate
            await retry_task("test-project/passed-task")

        mock_test_gate.assert_not_called()
