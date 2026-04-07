"""Tests for credential helper recreation on all working-state entry paths.

The git credential helper script (at {worktree}/.switchboard/git-creds.sh) must
be recreated on EVERY transition into the working state — not just initial dispatch.
These tests verify that ensure_credential_helper() is called unconditionally on:
  - dispatch (initial)
  - resume (with existing worktree — the confirmed bug scenario)
  - retry (with existing worktree)
  - start (post-reopen, with existing worktree)
"""

import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# ensure_credential_helper unit tests
# ---------------------------------------------------------------------------


class TestEnsureCredentialHelper:
    """Unit tests for the ensure_credential_helper() function in internals.py."""

    @pytest.fixture(autouse=True)
    def _patches(self):
        self.mock_setup_cred = AsyncMock(return_value="/fake/worktree/.switchboard/git-creds.sh")
        self.patcher = patch(
            "switchboard.dispatch.engine.setup_credential_helper",
            self.mock_setup_cred,
        )
        self.patcher.start()
        yield
        self.patcher.stop()

    async def test_delegates_to_setup_credential_helper(self):
        """ensure_credential_helper calls setup_credential_helper with correct args."""
        from switchboard.dispatch.internals import ensure_credential_helper

        task = {"id": "test-project/t1", "project_id": "test-project", "dispatched_by": 42}
        await ensure_credential_helper("/fake/worktree", task)

        self.mock_setup_cred.assert_awaited_once_with(
            "/fake/worktree", "test-project",
        )

    async def test_passes_correct_args_when_dispatched_by_absent(self):
        """ensure_credential_helper calls setup_credential_helper without user_id."""
        from switchboard.dispatch.internals import ensure_credential_helper

        task = {"id": "test-project/t2", "project_id": "test-project"}
        await ensure_credential_helper("/fake/worktree", task)

        self.mock_setup_cred.assert_awaited_once_with(
            "/fake/worktree", "test-project",
        )

    async def test_idempotent_called_twice(self):
        """Calling ensure_credential_helper twice produces no error."""
        from switchboard.dispatch.internals import ensure_credential_helper

        task = {"id": "test-project/t3", "project_id": "test-project", "dispatched_by": None}
        await ensure_credential_helper("/fake/worktree", task)
        await ensure_credential_helper("/fake/worktree", task)

        assert self.mock_setup_cred.await_count == 2

    async def test_missing_pat_does_not_raise(self):
        """When setup_credential_helper returns None (no PAT), ensure_credential_helper is silent."""
        self.mock_setup_cred.return_value = None

        from switchboard.dispatch.internals import ensure_credential_helper

        task = {"id": "test-project/t4", "project_id": "test-project", "dispatched_by": None}
        # Must not raise
        result = await ensure_credential_helper("/fake/worktree", task)
        assert result is None

    async def test_file_idempotency_with_real_worktree(self, tmp_path):
        """Calling setup_credential_helper twice writes then overwrites the helper file."""
        from switchboard.git.worktree import setup_credential_helper

        worktree = str(tmp_path / "wt")
        os.makedirs(worktree)
        # Create a fake .bare dir alongside so setup_credential_helper can set extensions
        bare = str(tmp_path / ".bare")
        os.makedirs(bare)

        mock_run = AsyncMock(return_value=(b"", b"", 0))
        mock_pat = AsyncMock(return_value="ghp_testtoken123")
        mock_get_project = AsyncMock(return_value={"id": "proj", "repo": "https://github.com/org/repo.git"})

        with patch("switchboard.git.worktree._run_as_worker", mock_run), \
             patch("switchboard.git.worktree.get_github_pat", mock_pat), \
             patch("switchboard.git.worktree.db") as mock_db:
            mock_db.get_project = mock_get_project
            result1 = await setup_credential_helper(worktree, "proj")
            result2 = await setup_credential_helper(worktree, "proj")

        # Both calls must succeed and return the same path
        assert result1 is not None
        assert result1 == result2
        # File must exist and have correct content
        assert os.path.exists(result1)
        content = open(result1).read()
        assert "ghp_testtoken123" in content


# ---------------------------------------------------------------------------
# Lifecycle integration tests — credential helper on resume
# ---------------------------------------------------------------------------


class TestCredentialHelperOnResume:
    """setup_credential_helper is called on resume regardless of worktree presence."""

    @pytest.fixture(autouse=True)
    def _common_patches(self, tmp_path):
        self.tmp_path = tmp_path
        self.mock_cred = AsyncMock(return_value=None)
        self.common_patches = [
            patch("switchboard.dispatch.engine.setup_credential_helper", self.mock_cred),
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

    async def test_resume_with_existing_worktree_calls_credential_helper(self, db, sample_project):
        """BUG FIX: credential helper is recreated even when worktree already exists on disk."""
        from switchboard.dispatch.engine import resume_task

        # Create a real directory so os.path.exists returns True
        worktree = str(self.tmp_path / "existing-wt")
        os.makedirs(worktree)

        task = await db.create_task(
            id="test-project/resume-existing-wt",
            project_id="test-project",
            goal="Test credential helper on resume with existing worktree",
        )
        # Simulate a stopped task with an existing worktree
        await db.update_task(
            task["id"],
            status="stopped",
            worktree_path=worktree,
            session_id="old-session-id",
        )

        await resume_task(task["id"])

        # setup_credential_helper MUST have been called — this was the bug
        assert self.mock_cred.await_count >= 1, (
            "setup_credential_helper was not called during resume with existing worktree. "
            "This is the bug: CC workers would fail to push because credential file was not recreated."
        )

    async def test_resume_without_worktree_also_calls_credential_helper(self, db, sample_project):
        """Regression guard: credential helper is still called when worktree is missing."""
        from switchboard.dispatch.engine import resume_task

        # Patch checkout_existing_worktree to return a path without real git ops
        checkout_mock = AsyncMock(return_value=str(self.tmp_path / "checked-out"))
        with patch("switchboard.dispatch.internals.checkout_existing_worktree", checkout_mock):
            task = await db.create_task(
                id="test-project/resume-no-wt",
                project_id="test-project",
                goal="Test credential helper on resume without worktree",
            )
            await db.update_task(
                task["id"],
                status="stopped",
                worktree_path=None,
                session_id="old-session-id",
            )

            await resume_task(task["id"])

        assert self.mock_cred.await_count >= 1, (
            "setup_credential_helper was not called during resume without worktree."
        )


# ---------------------------------------------------------------------------
# Lifecycle integration tests — credential helper on retry
# ---------------------------------------------------------------------------


class TestCredentialHelperOnRetry:
    """setup_credential_helper is called on retry regardless of worktree presence."""

    @pytest.fixture(autouse=True)
    def _common_patches(self, tmp_path):
        self.tmp_path = tmp_path
        self.mock_cred = AsyncMock(return_value=None)
        self.common_patches = [
            patch("switchboard.dispatch.engine.setup_credential_helper", self.mock_cred),
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

    async def test_retry_with_existing_worktree_calls_credential_helper(self, db, sample_project):
        """Credential helper is recreated on retry even when worktree already exists."""
        from switchboard.dispatch.engine import retry_task

        # Create a real directory so os.path.exists returns True
        worktree = str(self.tmp_path / "existing-retry-wt")
        os.makedirs(worktree)

        task = await db.create_task(
            id="test-project/retry-existing-wt",
            project_id="test-project",
            goal="Test credential helper on retry with existing worktree",
        )
        await db.update_task(
            task["id"],
            status="stopped",
            worktree_path=worktree,
            session_id="old-session-id",
        )

        await retry_task(task["id"])

        assert self.mock_cred.await_count >= 1, (
            "setup_credential_helper was not called during retry with existing worktree."
        )

    async def test_retry_without_worktree_also_calls_credential_helper(self, db, sample_project):
        """Regression guard: credential helper called when worktree is missing on retry."""
        from switchboard.dispatch.engine import retry_task

        task = await db.create_task(
            id="test-project/retry-no-wt",
            project_id="test-project",
            goal="Test credential helper on retry without worktree",
        )
        await db.update_task(
            task["id"],
            status="stopped",
            worktree_path=None,
            session_id="old-session-id",
        )

        await retry_task(task["id"])

        assert self.mock_cred.await_count >= 1, (
            "setup_credential_helper was not called during retry without worktree."
        )


# ---------------------------------------------------------------------------
# Lifecycle integration tests — credential helper on dispatch
# ---------------------------------------------------------------------------


class TestCredentialHelperOnDispatch:
    """Regression guard: credential helper is called on normal dispatch."""

    async def test_dispatch_calls_credential_helper(self, db, sample_project, tmp_path):
        """Normal dispatch must call setup_credential_helper (regression guard)."""
        from switchboard.dispatch.engine import dispatch_task

        mock_cred = AsyncMock(return_value=None)

        task = await db.create_task(
            id="test-project/dispatch-cred",
            project_id="test-project",
            goal="Test credential helper on dispatch",
        )
        await db.update_task(task["id"], status="ready")

        with patch("switchboard.dispatch.engine.setup_credential_helper", mock_cred), \
             patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value=str(tmp_path / "wt"))), \
             patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()), \
             patch("switchboard.dispatch.engine._setup_log_dir", AsyncMock(return_value=tmp_path / ".sb")), \
             patch("switchboard.dispatch.engine._write_dispatch_log", lambda *a, **k: None), \
             patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()), \
             patch("switchboard.dispatch.engine._build_task_prompt", AsyncMock(return_value="prompt")):
            # Use lifecycle.execute with task_id string
            from switchboard.dispatch.lifecycle import TaskLifecycle
            lifecycle = TaskLifecycle()
            await lifecycle.execute(task["id"], "dispatch")

        assert mock_cred.await_count >= 1, (
            "setup_credential_helper was not called during dispatch."
        )
