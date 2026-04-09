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


