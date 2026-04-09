"""Tests for process group isolation and prompt safety features.

Verifies:
1. anyio.open_process is patched to force start_new_session=True
2. The patch overrides any caller-supplied start_new_session=False
3. cancel_task still works correctly (uses asyncio cancellation, not process signals)
4. CLAUDE.md exists with required safety content
5. .claude/settings.json hook file exists with correct structure
"""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest


# ---------------------------------------------------------------------------
# Process group isolation — anyio.open_process patch
# ---------------------------------------------------------------------------

class TestAnyioOpenProcessPatch:

    def test_original_is_preserved(self):
        """The original anyio.open_process is saved as _orig_anyio_open_process."""
        import switchboard.dispatch.sdk_session as _sdk_session
        assert _sdk_session._orig_anyio_open_process is not None
        assert _sdk_session._orig_anyio_open_process is not _sdk_session._isolated_open_process

    async def test_patched_fn_forces_start_new_session_true(self):
        """_isolated_open_process always passes start_new_session=True."""
        import switchboard.dispatch.sdk_session as sdk_session
        captured_kwargs = {}

        async def mock_orig(command, **kwargs):
            captured_kwargs.update(kwargs)
            mock = MagicMock()
            mock.stdin = None
            mock.stdout = None
            mock.stderr = None
            return mock

        with patch.object(sdk_session, "_orig_anyio_open_process", mock_orig):
            await anyio.open_process(["echo", "hello"])

        assert captured_kwargs.get("start_new_session") is True


# ---------------------------------------------------------------------------
# cancel_task — unaffected by process isolation
# ---------------------------------------------------------------------------

class TestCancelTaskUnaffectedByIsolation:
    """cancel_task uses asyncio.Task.cancel(), not process signals.
    Process group isolation doesn't affect it.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, db):
        self.db = db


    async def test_cancel_task_cancels_asyncio_task(self, db, sample_project):
        """cancel_task finds and cancels the running asyncio Task by name."""
        from switchboard.dispatch.engine import cancel_task
        from switchboard.dispatch._state import _running_tasks

        # Create a long-running asyncio task with the expected name
        async def _long_task():
            await asyncio.sleep(9999)

        task_id = "test-project/cancel-asyncio-test"
        await db.create_task(
            id=task_id, project_id="test-project", goal="Long running",
        )
        await db.update_task(task_id, status="working")

        asyncio_task = asyncio.create_task(
            _long_task(), name=f"sdk-session-{task_id}"
        )
        _running_tasks.add(asyncio_task)

        try:
            result = await cancel_task(task_id)
            assert result["status"] == "cancelled"
            assert asyncio_task.cancelled() or asyncio_task.done()
        finally:
            _running_tasks.discard(asyncio_task)
            if not asyncio_task.done():
                asyncio_task.cancel()


# ---------------------------------------------------------------------------
# Safety files — CLAUDE.md and hook config
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent


class TestSafetyFiles:
    def test_claude_md_exists(self):
        """CLAUDE.md must exist at repo root."""
        assert (REPO_ROOT / "CLAUDE.md").exists(), "CLAUDE.md missing from repo root"

    def test_claude_md_has_safety_section(self):
        """CLAUDE.md must contain the safety instructions."""
        content = (REPO_ROOT / "CLAUDE.md").read_text()
        assert "SAFETY: Running tests and processes" in content
        assert "timeout" in content
        assert "kill" in content.lower()
        assert "pkill" in content
        assert "killall" in content

    def test_claude_md_has_timeout_guidance(self):
        """CLAUDE.md must tell workers to use timeout wrapper."""
        content = (REPO_ROOT / "CLAUDE.md").read_text()
        assert "timeout 60 pytest" in content or "timeout" in content

    def test_hook_settings_file_exists(self):
        """.claude/settings.json must exist at repo root."""
        settings_path = REPO_ROOT / ".claude" / "settings.json"
        assert settings_path.exists(), ".claude/settings.json missing from repo root"

    def test_hook_settings_valid_json(self):
        """.claude/settings.json must be valid JSON."""
        settings_path = REPO_ROOT / ".claude" / "settings.json"
        content = settings_path.read_text()
        data = json.loads(content)  # raises if invalid
        assert isinstance(data, dict)

    def test_hook_settings_has_pretooluse_hook(self):
        """.claude/settings.json must have a PreToolUse hook for Bash."""
        settings_path = REPO_ROOT / ".claude" / "settings.json"
        data = json.loads(settings_path.read_text())
        hooks = data.get("hooks", {})
        assert "PreToolUse" in hooks, "No PreToolUse hooks defined"
        pre_hooks = hooks["PreToolUse"]
        assert len(pre_hooks) > 0
        # At least one hook must match Bash
        bash_hooks = [h for h in pre_hooks if h.get("matcher") == "Bash"]
        assert len(bash_hooks) > 0, "No Bash PreToolUse hook defined"

    def test_hook_blocks_kill_commands(self):
        """The hook command must reference kill/pkill/killall blocking."""
        settings_path = REPO_ROOT / ".claude" / "settings.json"
        data = json.loads(settings_path.read_text())
        hooks = data["hooks"]["PreToolUse"]
        bash_hook = next(h for h in hooks if h.get("matcher") == "Bash")
        hook_commands = bash_hook.get("hooks", [])
        assert len(hook_commands) > 0
        # The hook command should reference kill blocking
        combined = " ".join(str(h.get("command", "")) for h in hook_commands)
        assert "pkill" in combined or "kill" in combined.lower()
        assert "BLOCKED" in combined


# ---------------------------------------------------------------------------
# Grounding prompt safety instructions
# ---------------------------------------------------------------------------

class TestGroundingPromptSafety:

    async def test_safety_section_present_on_revision(self, db, sample_project):
        """Safety instructions appear even on revision retries."""
        from switchboard.dispatch.sdk_session import _build_task_prompt

        task = await db.create_task(
            id="test-project/revision-safety-test",
            project_id="test-project",
            goal="Revision task",
        )
        review_feedback = [{"author": "stephen", "content": "Fix the thing", "type": "note"}]
        prompt = await _build_task_prompt(sample_project, task, None,
                                          review_feedback=review_feedback)

        assert "## Safety" in prompt
