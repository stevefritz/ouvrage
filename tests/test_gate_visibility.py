"""Tests for gate lifecycle visibility: streaming test output, subtask session log access."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# _run_test_streaming — streams output to file
# ---------------------------------------------------------------------------

class TestRunTestStreaming:
    """Verify that _run_test_streaming writes output to test-output.log in real time."""

    async def test_streams_output_to_file(self, tmp_path):
        """Output should appear in .switchboard/test-output.log."""
        worktree = str(tmp_path)
        (tmp_path / ".switchboard").mkdir()

        with patch("switchboard.dispatch.gates.pwd") as mock_pwd, \
             patch("switchboard.dispatch.gates.WORKER_USER", "nobody"):
            pw = MagicMock()
            pw.pw_uid = os.getuid()
            pw.pw_gid = os.getgid()
            pw.pw_dir = str(tmp_path)
            mock_pwd.getpwnam.return_value = pw

            from switchboard.dispatch.gates import _run_test_streaming
            output, rc = await _run_test_streaming(worktree, "echo hello && echo world")

        assert rc == 0
        assert "hello" in output
        assert "world" in output

        # Verify the file was written
        log_file = tmp_path / ".switchboard" / "test-output.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "hello" in content
        assert "world" in content

    async def test_captures_exit_code(self, tmp_path):
        """Non-zero exit code should be returned."""
        worktree = str(tmp_path)
        (tmp_path / ".switchboard").mkdir()

        with patch("switchboard.dispatch.gates.pwd") as mock_pwd, \
             patch("switchboard.dispatch.gates.WORKER_USER", "nobody"):
            pw = MagicMock()
            pw.pw_uid = os.getuid()
            pw.pw_gid = os.getgid()
            pw.pw_dir = str(tmp_path)
            mock_pwd.getpwnam.return_value = pw

            from switchboard.dispatch.gates import _run_test_streaming
            output, rc = await _run_test_streaming(worktree, "echo failing && exit 1")

        assert rc == 1
        assert "failing" in output


# ---------------------------------------------------------------------------
# _read_session_log — shared reader helper
# ---------------------------------------------------------------------------

class TestReadSessionLog:
    """Verify the JSONL session log reader helper."""

    async def test_reads_jsonl_with_tail(self, tmp_path):
        """Should read JSONL file and apply tail parameter."""
        log_path = tmp_path / "session.jsonl"
        entries = [{"type": "AssistantMessage", "content": [{"type": "text", "text": f"msg {i}"}]}
                   for i in range(10)]
        log_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        from switchboard.server.handlers.tasks import _read_session_log
        result = await _read_session_log(str(log_path), {"tail": 3}, "test-source")

        assert result["count"] == 3
        assert result["source"] == "test-source"
        # Should be the LAST 3 entries
        assert "msg 7" in result["entries"][0]["content"][0]["text"]

    async def test_filters_by_type(self, tmp_path):
        """Should filter entries by type when types parameter is given."""
        log_path = tmp_path / "session.jsonl"
        entries = [
            {"type": "AssistantMessage", "content": []},
            {"type": "UserMessage", "content": []},
            {"type": "AssistantMessage", "content": []},
        ]
        log_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

        from switchboard.server.handlers.tasks import _read_session_log
        result = await _read_session_log(str(log_path), {"types": "UserMessage"}, "test")

        assert result["count"] == 1
        assert result["entries"][0]["type"] == "UserMessage"

    async def test_truncates_large_content(self, tmp_path):
        """Should truncate text fields longer than 500 chars."""
        log_path = tmp_path / "session.jsonl"
        entry = {"type": "AssistantMessage", "content": [{"type": "text", "text": "x" * 1000}]}
        log_path.write_text(json.dumps(entry) + "\n")

        from switchboard.server.handlers.tasks import _read_session_log
        result = await _read_session_log(str(log_path), {}, "test")

        text = result["entries"][0]["content"][0]["text"]
        assert len(text) < 600  # 500 + truncation message
        assert "truncated" in text


# ---------------------------------------------------------------------------
# get_session_log — subtask ID resolution
# ---------------------------------------------------------------------------

class TestSubtaskSessionLog:
    """Verify that get_session_log handles subtask IDs like 'proj/task/review-1'."""

    async def test_subtask_id_resolves_to_parent_worktree(self, db, sample_project, tmp_path):
        """Subtask session log should be readable via the parent task's worktree."""
        # Create parent task with worktree
        worktree = str(tmp_path)
        task = await db.create_task(
            id="test-project/subtask-test",
            project_id="test-project",
            goal="Test subtask resolution",
        )
        await db.update_task(task["id"], worktree_path=worktree)

        # Create subtask
        await db.create_subtask(
            id="test-project/subtask-test/review-1",
            task_id=task["id"],
            type="review",
            prompt="Review this",
            model="opus",
        )

        # Write subtask session log
        log_dir = Path(worktree) / ".switchboard"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "review-1-session.jsonl"
        entry = {"type": "AssistantMessage", "content": [{"type": "text", "text": "reviewing code"}]}
        log_file.write_text(json.dumps(entry) + "\n")

        from switchboard.server.handlers.tasks import _handle_get_session_log
        result = await _handle_get_session_log({
            "task_id": "test-project/subtask-test/review-1",
            "tail": 50,
        })

        assert "error" not in result
        assert result["count"] == 1
        assert "reviewing code" in result["entries"][0]["content"][0]["text"]

    async def test_nonexistent_subtask_returns_error(self, db, sample_project):
        """Non-existent task/subtask ID returns an error."""
        from switchboard.server.handlers.tasks import _handle_get_session_log
        result = await _handle_get_session_log({
            "task_id": "test-project/nonexistent/review-1",
        })

        assert "error" in result


# ---------------------------------------------------------------------------
# Dashboard API: test-output and gate-session-log endpoints
# ---------------------------------------------------------------------------

class TestDashboardGateEndpoints:
    """Verify dashboard API serves gate-related files."""

    async def test_test_output_endpoint(self, db, sample_project, tmp_path):
        """GET /tasks/{id}/test-output should serve the test-output.log file."""
        worktree = str(tmp_path)
        task = await db.create_task(
            id="test-project/test-output-test",
            project_id="test-project",
            goal="Test output endpoint",
        )
        await db.update_task(task["id"], worktree_path=worktree)

        log_dir = Path(worktree) / ".switchboard"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "test-output.log").write_text("PASSED: test_foo\nPASSED: test_bar\n")

        from switchboard.dashboard.api import _handle_test_output

        # Mock the ASGI send
        responses = []
        async def mock_send(msg):
            responses.append(msg)

        scope = {"query_string": b""}
        await _handle_test_output(scope, mock_send, task["id"])

        # Find the body response
        body = next((r for r in responses if r.get("type") == "http.response.body"), None)
        assert body is not None
        assert b"PASSED: test_foo" in body["body"]

    async def test_gate_session_log_endpoint(self, db, sample_project, tmp_path):
        """GET /tasks/{id}/gate-session-log should serve the review subtask's session log."""
        worktree = str(tmp_path)
        task = await db.create_task(
            id="test-project/gate-log-test",
            project_id="test-project",
            goal="Gate session log endpoint",
        )
        await db.update_task(task["id"], worktree_path=worktree)

        # Create a review subtask
        await db.create_subtask(
            id="test-project/gate-log-test/review-1",
            task_id=task["id"],
            type="review",
            prompt="Review",
            model="opus",
        )

        # Write the subtask session log
        log_dir = Path(worktree) / ".switchboard"
        log_dir.mkdir(parents=True, exist_ok=True)
        entry = {"type": "AssistantMessage", "content": [{"type": "text", "text": "looks good"}]}
        (log_dir / "review-1-session.jsonl").write_text(json.dumps(entry) + "\n")

        from switchboard.dashboard.api import _handle_gate_session_log

        responses = []
        async def mock_send(msg):
            responses.append(msg)

        scope = {"query_string": b"type=review"}
        await _handle_gate_session_log(scope, mock_send, task["id"])

        body = next((r for r in responses if r.get("type") == "http.response.body"), None)
        assert body is not None
        parsed = json.loads(body["body"])
        assert len(parsed) == 1
        assert parsed[0]["content"][0]["text"] == "looks good"
