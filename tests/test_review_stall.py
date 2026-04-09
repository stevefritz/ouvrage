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


# ---------------------------------------------------------------------------
# retry_task gate pipeline re-run
# ---------------------------------------------------------------------------

