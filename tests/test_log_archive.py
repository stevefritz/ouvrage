"""Tests for log archive functionality.

Covers:
- archive_task_logs() creates correct folder structure and metadata.json
- archive called on retry, close, and release_worktree
- get_session_log / get_dispatch_log read from archive when attempt specified
- get_session_log / get_dispatch_log fall back to archive when worktree is gone
- list_attempts() returns sorted attempt list with metadata
- missing .switchboard/ is a no-op (not an error)
"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest


# Mock _run_as_worker to execute commands directly (no setuid in test context)
async def _fake_run_as_worker(*cmd, **kwargs):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return stdout, stderr, proc.returncode


@pytest.fixture(autouse=True)
def _mock_worker(monkeypatch):
    import switchboard.dispatch.engine as _engine
    monkeypatch.setattr(_engine, "_run_as_worker", _fake_run_as_worker)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(tmp_path, task_id="proj/my-task", dispatch_count=1, worktree=None):
    """Build a fake task dict with an optional worktree on disk."""
    wt = worktree or str(tmp_path / "my-task")
    return {
        "id": task_id,
        "project_id": "proj",
        "dispatch_count": dispatch_count,
        "worktree_path": wt,
        "session_id": "sess-abc",
        "total_cost_usd": 0.42,
        "total_input_tokens": 1000,
        "total_output_tokens": 500,
    }


def _make_project(tmp_path):
    return {
        "id": "proj",
        "working_dir": str(tmp_path / "proj"),
        "default_branch": "main",
    }


def _write_switchboard(worktree: str, session_lines=None, dispatch_text=None):
    """Create .switchboard/ folder with optional log files."""
    sb = Path(worktree) / ".switchboard"
    sb.mkdir(parents=True, exist_ok=True)
    if session_lines is not None:
        (sb / "session.jsonl").write_text("\n".join(json.dumps(l) for l in session_lines) + "\n")
    if dispatch_text is not None:
        (sb / "dispatch.log").write_text(dispatch_text)
    return sb


# ---------------------------------------------------------------------------
# archive_task_logs()
# ---------------------------------------------------------------------------

class TestArchiveTaskLogs:
    def setup_method(self):
        from switchboard.dispatch.engine import archive_task_logs
        self.archive = archive_task_logs

    @pytest.mark.asyncio
    async def test_creates_attempt_folder(self, tmp_path):
        task = _make_task(tmp_path, dispatch_count=1)
        project = _make_project(tmp_path)
        _write_switchboard(task["worktree_path"], session_lines=[{"type": "text", "content": "hi"}])

        result = await self.archive(task, project, "retry")

        dest = Path(project["working_dir"]) / ".task-history" / "my-task" / "attempt-1"
        assert dest.exists()
        assert result == dest

    @pytest.mark.asyncio
    async def test_copies_session_jsonl(self, tmp_path):
        task = _make_task(tmp_path, dispatch_count=2)
        project = _make_project(tmp_path)
        _write_switchboard(task["worktree_path"], session_lines=[{"type": "text"}, {"type": "tool"}])

        await self.archive(task, project, "retry")

        dest = Path(project["working_dir"]) / ".task-history" / "my-task" / "attempt-2"
        assert (dest / "session.jsonl").exists()

    @pytest.mark.asyncio
    async def test_copies_dispatch_log(self, tmp_path):
        task = _make_task(tmp_path, dispatch_count=1)
        project = _make_project(tmp_path)
        _write_switchboard(task["worktree_path"], dispatch_text="[ts] Dispatching task\n")

        await self.archive(task, project, "close")

        dest = Path(project["working_dir"]) / ".task-history" / "my-task" / "attempt-1"
        assert (dest / "dispatch.log").read_text() == "[ts] Dispatching task\n"

    @pytest.mark.asyncio
    async def test_writes_metadata_json(self, tmp_path):
        task = _make_task(tmp_path, dispatch_count=3)
        project = _make_project(tmp_path)
        _write_switchboard(task["worktree_path"])

        await self.archive(task, project, "detach")

        dest = Path(project["working_dir"]) / ".task-history" / "my-task" / "attempt-3"
        meta = json.loads((dest / "metadata.json").read_text())
        assert meta["task_id"] == "proj/my-task"
        assert meta["attempt"] == 3
        assert meta["reason"] == "detach"
        assert meta["session_id"] == "sess-abc"
        assert meta["cost_usd"] == pytest.approx(0.42)
        assert meta["input_tokens"] == 1000
        assert meta["output_tokens"] == 500
        assert "archived_at" in meta

    @pytest.mark.asyncio
    async def test_noop_when_no_worktree(self, tmp_path):
        task = {**_make_task(tmp_path), "worktree_path": None}
        project = _make_project(tmp_path)

        result = await self.archive(task, project, "retry")

        assert result is None
        assert not (Path(project["working_dir"]) / ".task-history").exists()

    @pytest.mark.asyncio
    async def test_noop_when_switchboard_missing(self, tmp_path):
        """If .switchboard/ doesn't exist in worktree, archive is a no-op."""
        task = _make_task(tmp_path)
        project = _make_project(tmp_path)
        # Create worktree dir but NO .switchboard/
        Path(task["worktree_path"]).mkdir(parents=True, exist_ok=True)

        result = await self.archive(task, project, "retry")

        assert result is None

    @pytest.mark.asyncio
    async def test_uses_dispatch_count_as_attempt_number(self, tmp_path):
        task = _make_task(tmp_path, dispatch_count=5)
        project = _make_project(tmp_path)
        _write_switchboard(task["worktree_path"])

        await self.archive(task, project, "completion")

        dest = Path(project["working_dir"]) / ".task-history" / "my-task" / "attempt-5"
        assert dest.exists()


# ---------------------------------------------------------------------------
# Call sites — retry, close, release_worktree
# ---------------------------------------------------------------------------

class TestArchiveCallSites:
    """Verify archive_task_logs is called from the right places."""

    @pytest.mark.asyncio
    async def test_retry_task_archives_before_dispatch(self, tmp_path, db, sample_project):
        """retry_task() should archive current attempt before dispatching."""
        import switchboard.dispatch.engine as _engine

        # Create a task with a worktree containing .switchboard/
        task = await db.create_task(
            id="test-project/retry-me",
            project_id="test-project",
            goal="Retry test",
            branch="retry-me",
        )
        task = await db.update_task(task["id"], status="needs-review", dispatch_count=1,
                                    worktree_path=str(tmp_path / "retry-me"))
        _write_switchboard(str(tmp_path / "retry-me"), dispatch_text="old dispatch log\n")

        archived_calls = []

        async def fake_archive(t, p, reason):
            archived_calls.append({"task_id": t["id"], "reason": reason})

        with patch("switchboard.dispatch.engine.archive_task_logs", side_effect=fake_archive), \
             patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value=str(tmp_path / "retry-me"))), \
             patch("switchboard.dispatch.engine._setup_log_dir", AsyncMock(return_value=tmp_path / "retry-me" / ".switchboard")), \
             patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()), \
             patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()), \
             patch("switchboard.dispatch.engine._write_dispatch_log"), \
             patch("switchboard.dispatch.engine.notify.task_dispatched", AsyncMock()):
            await _engine.retry_task("test-project/retry-me")

        assert len(archived_calls) == 1
        assert archived_calls[0]["reason"] == "retry"

    @pytest.mark.asyncio
    async def test_close_task_archives_before_cleanup(self, tmp_path, db, sample_project):
        """close_task() should archive before calling cleanup_worktree."""
        import switchboard.dispatch.engine as _engine

        task = await db.create_task(
            id="test-project/close-me",
            project_id="test-project",
            goal="Close test",
            branch="close-me",
        )
        # Use "stopped" — close is only valid from stopped state
        task = await db.update_task(task["id"], status="stopped", dispatch_count=2,
                                    worktree_path=str(tmp_path / "close-me"))
        _write_switchboard(str(tmp_path / "close-me"), dispatch_text="dispatch\n")

        archived_calls = []

        async def fake_archive(t, p, reason):
            archived_calls.append({"task_id": t["id"], "reason": reason})

        async def fake_close_side_effect(task, **ctx):
            """Replacement that calls our fake_archive but skips real cleanup."""
            project = await db.get_project(task["project_id"])
            if project:
                await fake_archive(task, project, "close")

        from switchboard.dispatch.lifecycle import TRANSITIONS
        tdef = TRANSITIONS[("stopped", "close")]
        orig_effects = tdef.side_effects[:]
        tdef.side_effects = [fake_close_side_effect, tdef.side_effects[-1]]  # keep post_close_message
        try:
            await _engine.close_task("test-project/close-me")
        finally:
            tdef.side_effects = orig_effects

        assert len(archived_calls) == 1
        assert archived_calls[0]["reason"] == "close"

    @pytest.mark.asyncio
    async def test_release_worktree_archives_before_detach(self, tmp_path, db, sample_project):
        """release_worktree() should archive before removing the worktree."""
        import switchboard.dispatch.engine as _engine

        task = await db.create_task(
            id="test-project/release-me",
            project_id="test-project",
            goal="Release test",
            branch="release-me",
        )
        task = await db.update_task(task["id"], status="completed", dispatch_count=1,
                                    worktree_path=str(tmp_path / "release-me"))
        _write_switchboard(str(tmp_path / "release-me"), dispatch_text="dispatch\n")

        archived_calls = []

        async def fake_archive(t, p, reason):
            archived_calls.append({"task_id": t["id"], "reason": reason})

        # Patch subprocess so worktree remove doesn't fail
        with patch("switchboard.dispatch.engine.archive_task_logs", side_effect=fake_archive), \
             patch("switchboard.dispatch.engine.asyncio.create_subprocess_exec", AsyncMock(
                 return_value=MagicMock(returncode=0, communicate=AsyncMock(return_value=(b"", b"")))
             )):
            await _engine.release_worktree("test-project/release-me")

        assert len(archived_calls) == 1
        assert archived_calls[0]["reason"] == "detach"

    @pytest.mark.asyncio
    async def test_auto_release_uses_completion_reason(self, tmp_path, db, sample_project):
        """_auto_release_worktree() should pass reason='completion' to release_worktree."""
        import switchboard.dispatch.engine as _engine

        task = await db.create_task(
            id="test-project/auto-release",
            project_id="test-project",
            goal="Auto release test",
            branch="auto-release",
        )
        task = await db.update_task(task["id"], status="completed", dispatch_count=1,
                                    worktree_path=str(tmp_path / "auto-release"),
                                    auto_release_worktree=1)

        release_calls = []

        async def fake_release(task_id, reason="detach"):
            release_calls.append({"task_id": task_id, "reason": reason})

        with patch("switchboard.dispatch.engine.release_worktree", side_effect=fake_release):
            await _engine._auto_release_worktree("test-project/auto-release")

        assert release_calls[0]["reason"] == "completion"


# ---------------------------------------------------------------------------
# list_attempts()
# ---------------------------------------------------------------------------

class TestListAttempts:
    def setup_method(self):
        from switchboard.dispatch.engine import list_attempts
        self.list_attempts = list_attempts

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_history(self, tmp_path, db, sample_project):
        task = await db.create_task(
            id="test-project/no-history",
            project_id="test-project",
            goal="No history",
        )

        with patch("switchboard.db.get_project", AsyncMock(return_value={
            "id": "test-project",
            "working_dir": str(tmp_path / "proj"),
        })):
            result = await self.list_attempts("test-project/no-history")

        assert result["task_id"] == "test-project/no-history"
        assert result["attempts"] == []

    @pytest.mark.asyncio
    async def test_returns_sorted_attempts(self, tmp_path, db, sample_project):
        from switchboard.dispatch.engine import archive_task_logs

        task_id = "test-project/multi-attempt"
        working_dir = str(tmp_path / "proj")
        project = {"id": "test-project", "working_dir": working_dir}

        # Simulate 3 archived attempts
        for attempt_num in [1, 2, 3]:
            task = {
                "id": task_id,
                "project_id": "test-project",
                "dispatch_count": attempt_num,
                "worktree_path": str(tmp_path / "multi-attempt"),
                "session_id": f"sess-{attempt_num}",
                "total_cost_usd": attempt_num * 0.1,
                "total_input_tokens": attempt_num * 100,
                "total_output_tokens": attempt_num * 50,
            }
            _write_switchboard(task["worktree_path"], dispatch_text=f"dispatch {attempt_num}\n")
            await archive_task_logs(task, project, "retry")

        await db.create_task(
            id=task_id,
            project_id="test-project",
            goal="Multi attempt task",
        )

        with patch("switchboard.db.get_project", AsyncMock(return_value=project)):
            result = await self.list_attempts(task_id)

        assert len(result["attempts"]) == 3
        # Sorted by attempt number
        assert result["attempts"][0]["attempt"] == 1
        assert result["attempts"][1]["attempt"] == 2
        assert result["attempts"][2]["attempt"] == 3

    @pytest.mark.asyncio
    async def test_metadata_fields_present(self, tmp_path, db, sample_project):
        from switchboard.dispatch.engine import archive_task_logs

        task_id = "test-project/metadata-check"
        working_dir = str(tmp_path / "proj")
        project = {"id": "test-project", "working_dir": working_dir}

        task = {
            "id": task_id,
            "project_id": "test-project",
            "dispatch_count": 1,
            "worktree_path": str(tmp_path / "metadata-check"),
            "session_id": "sess-xyz",
            "total_cost_usd": 1.23,
            "total_input_tokens": 500,
            "total_output_tokens": 250,
        }
        _write_switchboard(task["worktree_path"], dispatch_text="log\n")
        await archive_task_logs(task, project, "close")

        await db.create_task(id=task_id, project_id="test-project", goal="test")

        with patch("switchboard.db.get_project", AsyncMock(return_value=project)):
            result = await self.list_attempts(task_id)

        assert len(result["attempts"]) == 1
        meta = result["attempts"][0]
        assert meta["session_id"] == "sess-xyz"
        assert meta["reason"] == "close"
        assert meta["cost_usd"] == pytest.approx(1.23)
        assert "dispatch.log" in meta["files"]

    @pytest.mark.asyncio
    async def test_raises_for_unknown_task(self, tmp_path, db, sample_project):
        from switchboard.dispatch.engine import list_attempts
        with pytest.raises(ValueError, match="not found"):
            await list_attempts("test-project/does-not-exist")


# ---------------------------------------------------------------------------
# _find_archive_path()
# ---------------------------------------------------------------------------

class TestFindArchivePath:
    def setup_method(self):
        from switchboard.dispatch.engine import _find_archive_path
        self.fn = _find_archive_path

    def test_finds_specific_attempt(self, tmp_path):
        project = {"working_dir": str(tmp_path)}
        (tmp_path / ".task-history" / "my-task" / "attempt-2").mkdir(parents=True)

        result = self.fn(project, "proj/my-task", 2)
        assert result is not None
        assert result.name == "attempt-2"

    def test_returns_none_for_missing_attempt(self, tmp_path):
        project = {"working_dir": str(tmp_path)}
        result = self.fn(project, "proj/my-task", 99)
        assert result is None

    def test_finds_highest_when_none(self, tmp_path):
        project = {"working_dir": str(tmp_path)}
        for n in [1, 2, 3]:
            (tmp_path / ".task-history" / "my-task" / f"attempt-{n}").mkdir(parents=True)

        result = self.fn(project, "proj/my-task", None)
        assert result is not None
        assert result.name == "attempt-3"

    def test_returns_none_when_no_history(self, tmp_path):
        project = {"working_dir": str(tmp_path)}
        result = self.fn(project, "proj/my-task", None)
        assert result is None


# ---------------------------------------------------------------------------
# Historical log reading via server handlers
# ---------------------------------------------------------------------------

class TestHistoricalLogReading:
    """Test that get_session_log / get_dispatch_log read from archive when requested."""

    @pytest.mark.asyncio
    async def test_get_session_log_reads_from_archive(self, tmp_path, db, sample_project):
        from switchboard.server.handlers.tasks import _handle_get_session_log

        task_id = "test-project/archived-task"
        working_dir = str(tmp_path / "proj")
        project = {"id": "test-project", "working_dir": working_dir}

        # Create archive attempt-1 with session.jsonl
        dest = Path(working_dir) / ".task-history" / "archived-task" / "attempt-1"
        dest.mkdir(parents=True)
        entries = [{"type": "text", "content": [{"text": "hello"}]}, {"type": "result"}]
        (dest / "session.jsonl").write_text("\n".join(json.dumps(e) for e in entries))

        task = await db.create_task(id=task_id, project_id="test-project", goal="test")
        # No worktree
        await db.update_task(task_id, worktree_path=None)

        with patch("switchboard.db.get_project", AsyncMock(return_value=project)):
            result = await _handle_get_session_log({"task_id": task_id, "attempt": 1})

        assert "error" not in result
        assert result["count"] == 2
        assert "archive" in result["source"]

    @pytest.mark.asyncio
    async def test_get_session_log_falls_back_to_archive_when_no_worktree(self, tmp_path, db, sample_project):
        from switchboard.server.handlers.tasks import _handle_get_session_log

        task_id = "test-project/fallback-task"
        working_dir = str(tmp_path / "proj")
        project = {"id": "test-project", "working_dir": working_dir}

        # Create archive attempt-2 (latest)
        for n in [1, 2]:
            dest = Path(working_dir) / ".task-history" / "fallback-task" / f"attempt-{n}"
            dest.mkdir(parents=True)
            (dest / "session.jsonl").write_text(json.dumps({"type": "text", "attempt": n}) + "\n")

        task = await db.create_task(id=task_id, project_id="test-project", goal="test")
        await db.update_task(task_id, worktree_path=None)

        with patch("switchboard.db.get_project", AsyncMock(return_value=project)):
            # No attempt specified — should fall back to highest archive
            result = await _handle_get_session_log({"task_id": task_id})

        assert "error" not in result
        assert result["count"] == 1
        assert result["entries"][0]["attempt"] == 2  # latest attempt

    @pytest.mark.asyncio
    async def test_get_dispatch_log_reads_from_archive(self, tmp_path, db, sample_project):
        from switchboard.server.handlers.tasks import _handle_get_dispatch_log

        task_id = "test-project/dispatch-archived"
        working_dir = str(tmp_path / "proj")
        project = {"id": "test-project", "working_dir": working_dir}

        dest = Path(working_dir) / ".task-history" / "dispatch-archived" / "attempt-1"
        dest.mkdir(parents=True)
        (dest / "dispatch.log").write_text("[ts] old dispatch log\n")

        task = await db.create_task(id=task_id, project_id="test-project", goal="test")
        await db.update_task(task_id, worktree_path=None)

        with patch("switchboard.db.get_project", AsyncMock(return_value=project)):
            result = await _handle_get_dispatch_log({"task_id": task_id, "attempt": 1})

        assert "error" not in result
        assert "old dispatch log" in result["text"]
        assert "archive" in result["source"]

    @pytest.mark.asyncio
    async def test_get_session_log_error_when_no_archive_exists(self, tmp_path, db, sample_project):
        from switchboard.server.handlers.tasks import _handle_get_session_log

        task_id = "test-project/no-archive"
        working_dir = str(tmp_path / "proj")
        project = {"id": "test-project", "working_dir": working_dir}

        task = await db.create_task(id=task_id, project_id="test-project", goal="test")
        await db.update_task(task_id, worktree_path=None)

        with patch("switchboard.db.get_project", AsyncMock(return_value=project)):
            result = await _handle_get_session_log({"task_id": task_id})

        assert "error" in result


# ---------------------------------------------------------------------------
# Dashboard _resolve_dashboard_log_dir
# ---------------------------------------------------------------------------

class TestResolveDashboardLogDir:
    """Unit tests for dashboard_api._resolve_dashboard_log_dir."""

    @pytest.mark.asyncio
    async def test_returns_live_worktree_when_no_attempt(self, tmp_path, db):
        from switchboard.dashboard.api import _resolve_dashboard_log_dir

        wt = tmp_path / "my-task"
        sb = wt / ".switchboard"
        sb.mkdir(parents=True)

        task = {
            "id": "proj/my-task",
            "project_id": "proj",
            "worktree_path": str(wt),
        }
        project = {"id": "proj", "working_dir": str(tmp_path / "proj")}

        with patch("switchboard.db.get_project", AsyncMock(return_value=project)):
            result = await _resolve_dashboard_log_dir(task, attempt=None)

        assert result == sb

    @pytest.mark.asyncio
    async def test_returns_archive_when_attempt_specified(self, tmp_path, db):
        from switchboard.dashboard.api import _resolve_dashboard_log_dir

        working_dir = tmp_path / "proj"
        dest = working_dir / ".task-history" / "my-task" / "attempt-1"
        dest.mkdir(parents=True)

        task = {
            "id": "proj/my-task",
            "project_id": "proj",
            "worktree_path": None,
        }
        project = {"id": "proj", "working_dir": str(working_dir)}

        with patch("switchboard.db.get_project", AsyncMock(return_value=project)):
            result = await _resolve_dashboard_log_dir(task, attempt=1)

        assert result == dest

    @pytest.mark.asyncio
    async def test_falls_back_to_highest_archive_when_worktree_gone(self, tmp_path, db):
        from switchboard.dashboard.api import _resolve_dashboard_log_dir

        working_dir = tmp_path / "proj"
        for n in (1, 2):
            dest = working_dir / ".task-history" / "my-task" / f"attempt-{n}"
            dest.mkdir(parents=True)

        task = {
            "id": "proj/my-task",
            "project_id": "proj",
            "worktree_path": None,
        }
        project = {"id": "proj", "working_dir": str(working_dir)}

        with patch("switchboard.db.get_project", AsyncMock(return_value=project)):
            result = await _resolve_dashboard_log_dir(task, attempt=None)

        assert result == working_dir / ".task-history" / "my-task" / "attempt-2"

    @pytest.mark.asyncio
    async def test_returns_none_when_nothing_exists(self, tmp_path, db):
        from switchboard.dashboard.api import _resolve_dashboard_log_dir

        task = {
            "id": "proj/no-archive",
            "project_id": "proj",
            "worktree_path": None,
        }
        project = {"id": "proj", "working_dir": str(tmp_path / "proj")}

        with patch("switchboard.db.get_project", AsyncMock(return_value=project)):
            result = await _resolve_dashboard_log_dir(task, attempt=None)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_missing_attempt(self, tmp_path, db):
        from switchboard.dashboard.api import _resolve_dashboard_log_dir

        working_dir = tmp_path / "proj"
        # only attempt-1 exists, but we ask for attempt-5
        dest = working_dir / ".task-history" / "my-task" / "attempt-1"
        dest.mkdir(parents=True)

        task = {
            "id": "proj/my-task",
            "project_id": "proj",
            "worktree_path": None,
        }
        project = {"id": "proj", "working_dir": str(working_dir)}

        with patch("switchboard.db.get_project", AsyncMock(return_value=project)):
            result = await _resolve_dashboard_log_dir(task, attempt=5)

        assert result is None


# ---------------------------------------------------------------------------
# _filter_empty_text_entries
# ---------------------------------------------------------------------------

class TestFilterEmptyTextEntries:
    """Unit tests for dashboard_api._filter_empty_text_entries."""

    def test_removes_empty_text_assistant_message(self):
        from switchboard.dashboard.api import _filter_empty_text_entries

        entries = [
            {"type": "AssistantMessage", "content": [{"type": "text", "text": ""}]},
        ]
        assert _filter_empty_text_entries(entries) == []

    def test_removes_whitespace_only_text(self):
        from switchboard.dashboard.api import _filter_empty_text_entries

        entries = [
            {"type": "AssistantMessage", "content": [{"type": "text", "text": "   \n  "}]},
        ]
        assert _filter_empty_text_entries(entries) == []

    def test_keeps_non_empty_text_assistant_message(self):
        from switchboard.dashboard.api import _filter_empty_text_entries

        entry = {"type": "AssistantMessage", "content": [{"type": "text", "text": "hello"}]}
        assert _filter_empty_text_entries([entry]) == [entry]

    def test_keeps_tool_use_assistant_message_even_with_empty_text(self):
        from switchboard.dashboard.api import _filter_empty_text_entries

        # Has a tool_use block alongside an empty text block — classified as TOOL, not filtered
        entry = {
            "type": "AssistantMessage",
            "content": [
                {"type": "text", "text": ""},
                {"type": "tool_use", "name": "Read", "input": {}},
            ],
        }
        assert _filter_empty_text_entries([entry]) == [entry]

    def test_keeps_non_assistant_message_types(self):
        from switchboard.dashboard.api import _filter_empty_text_entries

        entries = [
            {"type": "UserMessage", "content": [{"type": "tool_result", "preview": ""}]},
            {"type": "SystemMessage", "subtype": "init"},
            {"type": "ResultMessage", "num_turns": 5},
        ]
        assert _filter_empty_text_entries(entries) == entries

    def test_removes_empty_content_list(self):
        from switchboard.dashboard.api import _filter_empty_text_entries

        # AssistantMessage with no content blocks at all — treated as empty
        entries = [{"type": "AssistantMessage", "content": []}]
        assert _filter_empty_text_entries(entries) == []

    def test_mixed_entries_filters_only_empty_text(self):
        from switchboard.dashboard.api import _filter_empty_text_entries

        empty_text = {"type": "AssistantMessage", "content": [{"type": "text", "text": ""}]}
        real_text = {"type": "AssistantMessage", "content": [{"type": "text", "text": "doing work"}]}
        tool_call = {
            "type": "AssistantMessage",
            "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
        }
        system = {"type": "SystemMessage", "subtype": "init"}

        result = _filter_empty_text_entries([empty_text, real_text, tool_call, system])
        assert result == [real_text, tool_call, system]
