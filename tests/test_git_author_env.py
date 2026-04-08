"""Tests for git author env var injection (_resolve_git_author in sdk_session.py).

Verifies that GIT_AUTHOR_NAME/EMAIL and GIT_COMMITTER_NAME/EMAIL are set
from the task owner's profile, with correct fallback chain.
"""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import switchboard.db as db


# ---------------------------------------------------------------------------
# _resolve_git_author unit tests
# ---------------------------------------------------------------------------

class TestResolveGitAuthor:
    """Test the fallback chain in _resolve_git_author."""

    async def test_uses_dispatched_by_name_and_email(self, db, sample_project):
        """When dispatched_by user has name and email, use them."""
        from switchboard.dispatch.sdk_session import _resolve_git_author

        user = await db.create_user(
            email="alice@example.com", name="Alice Smith",
        )
        task = await db.create_task(
            id="test-project/author-dispatched",
            project_id="test-project",
            goal="Test git author",
            dispatched_by=user["id"],
        )

        result = await _resolve_git_author(task["id"])

        assert result["GIT_AUTHOR_NAME"] == "Alice Smith"
        assert result["GIT_AUTHOR_EMAIL"] == "alice@example.com"
        assert result["GIT_COMMITTER_NAME"] == "Alice Smith"
        assert result["GIT_COMMITTER_EMAIL"] == "alice@example.com"

    async def test_uses_created_by_when_no_dispatched_by(self, db, sample_project):
        """When dispatched_by is None, fall back to created_by user."""
        from switchboard.dispatch.sdk_session import _resolve_git_author

        user = await db.create_user(
            email="bob@example.com", name="Bob Jones",
        )
        task = await db.create_task(
            id="test-project/author-created-by",
            project_id="test-project",
            goal="Test git author fallback to created_by",
            created_by=user["id"],
            dispatched_by=None,
        )

        result = await _resolve_git_author(task["id"])

        assert result["GIT_AUTHOR_NAME"] == "Bob Jones"
        assert result["GIT_AUTHOR_EMAIL"] == "bob@example.com"
        assert result["GIT_COMMITTER_NAME"] == "Bob Jones"
        assert result["GIT_COMMITTER_EMAIL"] == "bob@example.com"

    async def test_dispatched_by_takes_priority_over_created_by(self, db, sample_project):
        """dispatched_by has higher priority than created_by."""
        from switchboard.dispatch.sdk_session import _resolve_git_author

        creator = await db.create_user(email="creator@example.com", name="Creator User")
        dispatcher = await db.create_user(email="dispatcher@example.com", name="Dispatcher User")
        task = await db.create_task(
            id="test-project/author-priority",
            project_id="test-project",
            goal="Test priority",
            created_by=creator["id"],
            dispatched_by=dispatcher["id"],
        )

        result = await _resolve_git_author(task["id"])

        assert result["GIT_AUTHOR_NAME"] == "Dispatcher User"
        assert result["GIT_AUTHOR_EMAIL"] == "dispatcher@example.com"

    async def test_fallback_to_instance_owner_when_user_has_no_name(self, db, sample_project):
        """If task user has no name, fall back to instance owner."""
        from switchboard.dispatch.sdk_session import _resolve_git_author

        # Create user without a name (empty string)
        nameless = await db.create_user(email="nameless@example.com", name="")
        owner = await db.create_user(email="owner@example.com", name="Instance Owner")

        # Point instance to owner
        await db.update_instance(owner_user_id=owner["id"])

        task = await db.create_task(
            id="test-project/author-no-name",
            project_id="test-project",
            goal="Test fallback to instance owner",
            dispatched_by=nameless["id"],
        )

        result = await _resolve_git_author(task["id"])

        assert result["GIT_AUTHOR_NAME"] == "Instance Owner"
        assert result["GIT_AUTHOR_EMAIL"] == "owner@example.com"

    async def test_fallback_to_bot_when_no_user_and_no_instance_owner(self, db, sample_project):
        """If no user identity available anywhere, use Ouvrage Bot defaults."""
        from switchboard.dispatch.sdk_session import _resolve_git_author

        task = await db.create_task(
            id="test-project/author-bot-fallback",
            project_id="test-project",
            goal="Test bot fallback",
            dispatched_by=None,
            created_by=None,
        )

        # Ensure instance has no owner_user_id
        await db.update_instance(owner_user_id=None)

        result = await _resolve_git_author(task["id"])

        assert result["GIT_AUTHOR_NAME"] == "Ouvrage Bot"
        assert result["GIT_AUTHOR_EMAIL"] == "bot@ouvrage.build"
        assert result["GIT_COMMITTER_NAME"] == "Ouvrage Bot"
        assert result["GIT_COMMITTER_EMAIL"] == "bot@ouvrage.build"

    async def test_fallback_to_bot_when_task_not_found(self, db):
        """If the task doesn't exist, return bot defaults without crashing."""
        from switchboard.dispatch.sdk_session import _resolve_git_author

        # Clear instance owner so bot fallback is reached
        await db.update_instance(owner_user_id=None)

        result = await _resolve_git_author("test-project/nonexistent-task")

        assert result["GIT_AUTHOR_NAME"] == "Ouvrage Bot"
        assert result["GIT_AUTHOR_EMAIL"] == "bot@ouvrage.build"

    async def test_all_four_env_vars_present(self, db, sample_project):
        """All four git env vars must always be returned."""
        from switchboard.dispatch.sdk_session import _resolve_git_author

        task = await db.create_task(
            id="test-project/author-all-vars",
            project_id="test-project",
            goal="Test all four vars",
        )
        result = await _resolve_git_author(task["id"])

        assert set(result.keys()) == {
            "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
        }


# ---------------------------------------------------------------------------
# Integration: env vars appear in ClaudeAgentOptions passed to SDK
# ---------------------------------------------------------------------------

class TestGitAuthorEnvInjectedIntoSdkSession:
    """Verify git author env vars flow into the ClaudeAgentOptions env dict."""

    @pytest.fixture(autouse=True)
    def _patches(self, tmp_path):
        import pwd

        self.log_dir = tmp_path / "logs"
        self.log_dir.mkdir()
        self.worktree = str(tmp_path / "wt")
        os.makedirs(self.worktree, exist_ok=True)

        mock_pw = MagicMock()
        mock_pw.pw_dir = str(tmp_path)

        from claude_agent_sdk import ResultMessage as _RM
        result_msg = MagicMock(spec=_RM)
        result_msg.is_error = False
        result_msg.result = "Done."
        result_msg.stop_reason = "end_turn"
        result_msg.num_turns = 1
        result_msg.total_cost_usd = 0.001
        result_msg.duration_ms = 5000
        result_msg.duration_api_ms = 4800
        result_msg.session_id = None
        result_msg.usage = {
            "input_tokens": 10, "output_tokens": 5,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        }

        async def _fast_gen():
            yield result_msg

        self.captured_options = []

        def _capture_options(options):
            self.captured_options.append(options)
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.query = AsyncMock()
            mock_client.receive_response = MagicMock(return_value=_fast_gen())
            return mock_client

        patches = [
            patch("switchboard.dispatch.sdk_session.ClaudeSDKClient",
                  side_effect=_capture_options),
            patch("switchboard.git.operations._ensure_branch_pushed",
                  AsyncMock(return_value=False)),
            patch("switchboard.dispatch.gates._run_test_gate", AsyncMock()),
            patch("switchboard.dispatch.gates._dispatch_review", AsyncMock()),
            patch("switchboard.dispatch.engine._check_and_dispatch_dependents", AsyncMock()),
            patch("switchboard.dispatch.engine._update_usage", AsyncMock()),
            patch("switchboard.dispatch.sdk_session.pwd.getpwnam", return_value=mock_pw),
            patch("switchboard.notifications.slack.task_completed", AsyncMock()),
            patch("switchboard.notifications.slack.task_needs_review", AsyncMock()),
            patch("switchboard.dispatch.queue._drain_queue", AsyncMock()),
        ]
        for p in patches:
            p.start()
        self._patches = patches
        yield
        for p in patches:
            p.stop()

    async def test_git_env_vars_set_from_dispatched_by(self, db, sample_project):
        """GIT_AUTHOR/COMMITTER env vars are injected with the dispatched_by user's info."""
        from switchboard.dispatch.sdk_session import _run_sdk_session

        user = await db.create_user(email="carol@example.com", name="Carol Dev")
        task = await db.create_task(
            id="test-project/env-dispatched",
            project_id="test-project",
            goal="Test env injection",
            auto_test=False,
            auto_review=False,
            dispatched_by=user["id"],
        )
        await db.update_task(task["id"], status="working", worktree_path=self.worktree)

        await _run_sdk_session(
            task_id=task["id"],
            prompt="do the thing",
            worktree_path=self.worktree,
            session_id=None,
            is_resume=False,
            max_turns=10,
            max_wall_clock_minutes=30,
            log_dir=self.log_dir,
        )

        assert self.captured_options, "ClaudeSDKClient was never called"
        env = self.captured_options[0].env
        assert env.get("GIT_AUTHOR_NAME") == "Carol Dev"
        assert env.get("GIT_AUTHOR_EMAIL") == "carol@example.com"
        assert env.get("GIT_COMMITTER_NAME") == "Carol Dev"
        assert env.get("GIT_COMMITTER_EMAIL") == "carol@example.com"

    async def test_git_env_vars_fallback_to_bot(self, db, sample_project):
        """When no user identity, GIT_* env vars fall back to Ouvrage Bot."""
        from switchboard.dispatch.sdk_session import _run_sdk_session

        task = await db.create_task(
            id="test-project/env-bot-fallback",
            project_id="test-project",
            goal="Test bot fallback env injection",
            auto_test=False,
            auto_review=False,
            dispatched_by=None,
            created_by=None,
        )
        await db.update_task(task["id"], status="working", worktree_path=self.worktree)

        # Ensure no instance owner
        await db.update_instance(owner_user_id=None)

        await _run_sdk_session(
            task_id=task["id"],
            prompt="do the thing",
            worktree_path=self.worktree,
            session_id=None,
            is_resume=False,
            max_turns=10,
            max_wall_clock_minutes=30,
            log_dir=self.log_dir,
        )

        assert self.captured_options, "ClaudeSDKClient was never called"
        env = self.captured_options[0].env
        assert env.get("GIT_AUTHOR_NAME") == "Ouvrage Bot"
        assert env.get("GIT_AUTHOR_EMAIL") == "bot@ouvrage.build"
        assert env.get("GIT_COMMITTER_NAME") == "Ouvrage Bot"
        assert env.get("GIT_COMMITTER_EMAIL") == "bot@ouvrage.build"
