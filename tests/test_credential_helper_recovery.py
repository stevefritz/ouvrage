"""Tests for hook config setup on all working-state entry paths.

The PreToolUse hook config (blocking direct git push/fetch) must be written
on EVERY transition into the working state — not just initial dispatch.
These tests verify that setup_hook_config() is called unconditionally on:
  - dispatch (initial)
  - resume (with existing worktree — the confirmed pattern)
  - retry (with existing worktree)
  - start (post-reopen, with existing worktree)
"""

import json
import os
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# setup_hook_config unit tests
# ---------------------------------------------------------------------------


class TestSetupHookConfig:
    """Unit tests for the setup_hook_config() function in internals.py."""

    async def test_writes_hook_config_to_empty_dir(self, tmp_path):
        """setup_hook_config creates .claude/settings.json with hook entries."""
        from switchboard.dispatch.internals import setup_hook_config

        worktree = str(tmp_path / "wt")
        os.makedirs(worktree)

        await setup_hook_config(worktree)

        settings_path = os.path.join(worktree, ".claude", "settings.json")
        assert os.path.exists(settings_path)

        with open(settings_path) as f:
            settings = json.load(f)

        hooks = settings["hooks"]["PreToolUse"]
        assert len(hooks) == 1
        assert hooks[0]["matcher"] == "Bash"
        hook_cmds = [h["command"] for h in hooks[0]["hooks"]]
        assert "/opt/switchboard/hooks/block-git-push.sh" in hook_cmds
        assert "/opt/switchboard/hooks/block-git-fetch.sh" in hook_cmds

    async def test_merges_with_existing_settings(self, tmp_path):
        """setup_hook_config preserves existing settings and adds hooks."""
        from switchboard.dispatch.internals import setup_hook_config

        worktree = str(tmp_path / "wt")
        claude_dir = os.path.join(worktree, ".claude")
        os.makedirs(claude_dir)

        # Write existing settings
        existing = {
            "includeCoAuthoredBy": False,
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [
                            {"type": "command", "command": "existing-hook.sh"}
                        ],
                    }
                ]
            },
        }
        with open(os.path.join(claude_dir, "settings.json"), "w") as f:
            json.dump(existing, f)

        await setup_hook_config(worktree)

        with open(os.path.join(claude_dir, "settings.json")) as f:
            settings = json.load(f)

        # Existing setting preserved
        assert settings["includeCoAuthoredBy"] is False

        # Existing hook preserved, new hooks added as separate entry
        hooks = settings["hooks"]["PreToolUse"]
        assert len(hooks) == 2  # original entry + new entry

        # The existing hook entry is untouched
        assert hooks[0]["hooks"][0]["command"] == "existing-hook.sh"

        # New entry has both blocking hooks
        new_cmds = [h["command"] for h in hooks[1]["hooks"]]
        assert "/opt/switchboard/hooks/block-git-push.sh" in new_cmds
        assert "/opt/switchboard/hooks/block-git-fetch.sh" in new_cmds

    async def test_idempotent_called_twice(self, tmp_path):
        """Calling setup_hook_config twice does not duplicate hooks."""
        from switchboard.dispatch.internals import setup_hook_config

        worktree = str(tmp_path / "wt")
        os.makedirs(worktree)

        await setup_hook_config(worktree)
        await setup_hook_config(worktree)

        with open(os.path.join(worktree, ".claude", "settings.json")) as f:
            settings = json.load(f)

        hooks = settings["hooks"]["PreToolUse"]
        # Should still be just one entry with our hooks
        push_count = sum(
            1 for entry in hooks for h in entry.get("hooks", [])
            if h.get("command", "").endswith("block-git-push.sh")
        )
        assert push_count == 1

    async def test_handles_corrupt_settings_json(self, tmp_path):
        """setup_hook_config recovers from corrupt settings.json."""
        from switchboard.dispatch.internals import setup_hook_config

        worktree = str(tmp_path / "wt")
        claude_dir = os.path.join(worktree, ".claude")
        os.makedirs(claude_dir)

        # Write corrupt JSON
        with open(os.path.join(claude_dir, "settings.json"), "w") as f:
            f.write("{invalid json")

        await setup_hook_config(worktree)

        with open(os.path.join(claude_dir, "settings.json")) as f:
            settings = json.load(f)

        # Should have fresh hooks
        assert "hooks" in settings
        assert "PreToolUse" in settings["hooks"]


# ---------------------------------------------------------------------------
# Lifecycle integration tests — hook config on resume
# ---------------------------------------------------------------------------


class TestHookConfigOnResume:
    """setup_hook_config is called on resume regardless of worktree presence."""

    @pytest.fixture(autouse=True)
    def _common_patches(self, tmp_path):
        self.tmp_path = tmp_path
        self.mock_hook_config = AsyncMock()
        self.common_patches = [
            patch("switchboard.dispatch.internals.setup_hook_config", self.mock_hook_config),
                        patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value=str(tmp_path / "wt"))),
            patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()),
            patch("switchboard.dispatch.sdk_session._build_resume_prompt", AsyncMock(return_value="resume prompt")),
            patch("switchboard.dispatch.engine._setup_log_dir", AsyncMock(return_value=tmp_path / ".sb")),
            patch("switchboard.dispatch.engine._write_dispatch_log", lambda *a, **k: None),
            patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()),
        ]
        for p in self.common_patches:
            p.start()
        yield
        for p in self.common_patches:
            p.stop()

    async def test_resume_with_existing_worktree_calls_hook_config(self, db, sample_project):
        """Hook config is written even when worktree already exists on disk."""
        from switchboard.dispatch.engine import resume_task

        # Create a real directory so os.path.exists returns True
        worktree = str(self.tmp_path / "existing-wt")
        os.makedirs(worktree)

        task = await db.create_task(
            id="test-project/resume-existing-wt",
            project_id="test-project",
            goal="Test hook config on resume with existing worktree",
        )
        await db.update_task(
            task["id"],
            status="stopped",
            worktree_path=worktree,
            session_id="old-session-id",
        )

        await resume_task(task["id"])

        assert self.mock_hook_config.await_count >= 1, (
            "setup_hook_config was not called during resume with existing worktree."
        )


# ---------------------------------------------------------------------------
# Lifecycle integration tests — hook config on retry
# ---------------------------------------------------------------------------


class TestHookConfigOnRetry:
    """setup_hook_config is called on retry regardless of worktree presence."""

    @pytest.fixture(autouse=True)
    def _common_patches(self, tmp_path):
        self.tmp_path = tmp_path
        self.mock_hook_config = AsyncMock()
        self.common_patches = [
            patch("switchboard.dispatch.internals.setup_hook_config", self.mock_hook_config),
                        patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value=str(tmp_path / "wt"))),
            patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()),
            patch("switchboard.dispatch.engine.archive_task_logs", AsyncMock()),
            patch("switchboard.dispatch.engine._invalidate_chain", AsyncMock()),
            patch("switchboard.dispatch.engine._setup_log_dir", AsyncMock(return_value=tmp_path / ".sb")),
            patch("switchboard.dispatch.engine._write_dispatch_log", lambda *a, **k: None),
            patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()),
            patch("switchboard.dispatch.engine._build_task_prompt", AsyncMock(return_value="prompt")),
        ]
        for p in self.common_patches:
            p.start()
        yield
        for p in self.common_patches:
            p.stop()

    async def test_retry_with_existing_worktree_calls_hook_config(self, db, sample_project):
        """Hook config is written on retry even when worktree already exists."""
        from switchboard.dispatch.engine import retry_task

        worktree = str(self.tmp_path / "existing-retry-wt")
        os.makedirs(worktree)

        task = await db.create_task(
            id="test-project/retry-existing-wt",
            project_id="test-project",
            goal="Test hook config on retry with existing worktree",
        )
        await db.update_task(
            task["id"],
            status="stopped",
            worktree_path=worktree,
            session_id="old-session-id",
        )

        await retry_task(task["id"])

        assert self.mock_hook_config.await_count >= 1, (
            "setup_hook_config was not called during retry with existing worktree."
        )


# ---------------------------------------------------------------------------
# Lifecycle integration tests — hook config on dispatch
# ---------------------------------------------------------------------------


class TestHookConfigOnDispatch:
    """Hook config is written on normal dispatch."""

    async def test_dispatch_calls_hook_config(self, db, sample_project, tmp_path):
        """Normal dispatch must call setup_hook_config."""
        from switchboard.dispatch.engine import dispatch_task

        mock_hook_config = AsyncMock()

        task = await db.create_task(
            id="test-project/dispatch-hook",
            project_id="test-project",
            goal="Test hook config on dispatch",
        )
        await db.update_task(task["id"], status="ready")

        with patch("switchboard.dispatch.internals.setup_hook_config", mock_hook_config), \
             patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value=str(tmp_path / "wt"))), \
             patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()), \
             patch("switchboard.dispatch.engine._setup_log_dir", AsyncMock(return_value=tmp_path / ".sb")), \
             patch("switchboard.dispatch.engine._write_dispatch_log", lambda *a, **k: None), \
             patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()), \
             patch("switchboard.dispatch.engine._build_task_prompt", AsyncMock(return_value="prompt")):
            from switchboard.dispatch.lifecycle import TaskLifecycle
            lifecycle = TaskLifecycle()
            await lifecycle.execute(task["id"], "dispatch")

        assert mock_hook_config.await_count >= 1, (
            "setup_hook_config was not called during dispatch."
        )
