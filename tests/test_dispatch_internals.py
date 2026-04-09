"""Tests for switchboard.dispatch.internals — status-agnostic dispatch building blocks.

Each extracted function is tested in isolation with mocked DB/git operations.
"""

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import switchboard.db as db


# ---------------------------------------------------------------------------
# setup_task_worktree
# ---------------------------------------------------------------------------

class TestSetupTaskWorktree:
    @pytest.fixture(autouse=True)
    def _patches(self):
        self.setup_worktree_mock = AsyncMock(return_value="/tmp/fake-worktree")
        self.setup_hook_mock = AsyncMock()
        self.run_setup_mock = AsyncMock()
        # Patch on engine's namespace since internals reads through engine.*
        patches = [
            patch("switchboard.dispatch.engine.setup_worktree", self.setup_worktree_mock),
            patch("switchboard.dispatch.internals.setup_hook_config", self.setup_hook_mock),
            patch("switchboard.dispatch.engine.run_setup_command", self.run_setup_mock),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# resolve_session_config
# ---------------------------------------------------------------------------

class TestResolveSessionConfig:
    def test_task_overrides_project(self):
        from switchboard.dispatch.internals import resolve_session_config

        task = {"max_turns": 50, "max_wall_clock": 30, "model": "haiku"}
        project = {"max_turns": 150, "max_wall_clock": 45, "model": "opus"}
        config = resolve_session_config(task, project)

        assert config["max_turns"] == 50
        assert config["max_wall_clock"] == 30
        assert config["model"] == "haiku"


# ---------------------------------------------------------------------------
# build_dispatch_prompt
# ---------------------------------------------------------------------------

class TestBuildDispatchPrompt:
    @pytest.fixture(autouse=True)
    def _patches(self):
        self.build_prompt_mock = AsyncMock(return_value="mock prompt")
        patches = [
            patch("switchboard.dispatch.engine._build_task_prompt", self.build_prompt_mock),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()

    async def test_with_spec(self, db, sample_project):
        from switchboard.dispatch.internals import build_dispatch_prompt

        task = await db.create_task(
            id="test-project/prompt-task", project_id="test-project",
            goal="Test",
        )
        # Pin a spec message
        await db.post_task_message(
            task_id="test-project/prompt-task", author="dispatcher",
            content="Build the feature", type="spec", pinned=True,
        )

        result = await build_dispatch_prompt(sample_project, task)
        assert result == "mock prompt"

        # Verify _build_task_prompt was called with spec_content
        call_args = self.build_prompt_mock.call_args
        assert call_args[0][2] == "Build the feature"  # spec_content


# ---------------------------------------------------------------------------
# launch_sdk_session
# ---------------------------------------------------------------------------

class TestLaunchSdkSession:
    @pytest.fixture(autouse=True)
    def _patches(self):
        self.setup_log_mock = AsyncMock(return_value="/tmp/log-dir")
        self.write_log_mock = MagicMock()
        self.run_session_mock = AsyncMock()
        self.handle_exc_mock = MagicMock()
        patches = [
            patch("switchboard.dispatch.engine._setup_log_dir", self.setup_log_mock),
            patch("switchboard.dispatch.engine._write_dispatch_log", self.write_log_mock),
            patch("switchboard.dispatch.engine._run_sdk_session", self.run_session_mock),
            patch("switchboard.dispatch.engine._handle_task_exception", self.handle_exc_mock),
        ]
        for p in patches:
            p.start()
        yield
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# check_and_queue_if_full
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# collect_review_feedback
# ---------------------------------------------------------------------------

class TestCollectReviewFeedback:
    async def test_found(self, db, sample_project):
        from switchboard.dispatch.internals import collect_review_feedback

        task = await db.create_task(
            id="test-project/review-fb", project_id="test-project",
            goal="Test",
        )
        # Post a result message then feedback
        await db.post_task_message(
            task_id="test-project/review-fb", author="cc-worker",
            content="Done", type="result",
        )
        await db.post_task_message(
            task_id="test-project/review-fb", author="user",
            content="Please fix the typo", type="note",
        )

        result = await collect_review_feedback("test-project/review-fb")
        assert result is not None
        assert len(result) == 1
        assert result[0]["content"] == "Please fix the typo"


# ---------------------------------------------------------------------------
# collect_reopen_feedback
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# setup_hook_config
# ---------------------------------------------------------------------------

