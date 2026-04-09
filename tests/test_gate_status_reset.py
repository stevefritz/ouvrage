"""Tests for gate_status and gate_retries reset on retry/resume.

Regression: stale gate_status from a previous attempt was persisting when
retry_task or resume_task started a new attempt, causing the dashboard to
show "TEST FAILED" while CC was still writing code on the new attempt.
"""

from unittest.mock import AsyncMock, patch

import pytest


class TestRetryResetsGateStatus:
    """retry_task must reset gate_status and gate_retries on new attempt."""

    async def test_retry_resets_gate_status(self, db, sample_project):
        """gate_status must be null after retry_task starts a new attempt."""
        from switchboard.dispatch.engine import retry_task

        task = await db.create_task(
            id="test-project/retry-gate-reset",
            project_id="test-project",
            goal="Test retry resets gate_status",
        )
        await db.update_task(task["id"], status="failed", gate_status="test-failed", gate_retries=2)

        with patch("switchboard.dispatch.engine.dispatch_task", AsyncMock(return_value={"status": "working"})):
            with patch("switchboard.dispatch.engine._invalidate_chain", AsyncMock()):
                await retry_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["gate_status"] is None, (
            f"Expected gate_status=None after retry, got {updated['gate_status']!r}"
        )


class TestResumeResetsGateStatus:
    """resume_task must PRESERVE gate_status and gate_retries (Bug #2 fix).

    After the Bug #2 fix, resume no longer clears gate_status/gate_retries —
    these are preserved so the dashboard can still show the last gate outcome
    during the resumed session.
    """


    async def test_resume_preserves_gate_retries(self, db, sample_project):
        """gate_retries is preserved (not cleared) after resume_task."""
        from switchboard.dispatch.engine import resume_task

        task = await db.create_task(
            id="test-project/resume-gate-retries-reset",
            project_id="test-project",
            goal="Test resume preserves gate_retries",
        )
        await db.update_task(task["id"], status="needs-review", gate_status="test-failed", gate_retries=1)

        with patch("switchboard.dispatch.engine.setup_worktree", AsyncMock(return_value="/tmp/fake-wt")), \
             patch("switchboard.dispatch.internals.setup_hook_config", AsyncMock()), \
             patch("switchboard.dispatch.engine.run_setup_command", AsyncMock()), \
             patch("switchboard.dispatch.sdk_session._build_resume_prompt", AsyncMock(return_value="prompt")), \
             patch("switchboard.dispatch.engine._setup_log_dir", AsyncMock(return_value="/tmp/fake-wt/.switchboard")), \
             patch("switchboard.dispatch.engine._write_dispatch_log"), \
             patch("switchboard.dispatch.engine._run_sdk_session", AsyncMock()):
            await resume_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["gate_retries"] == 1, (
            f"Expected gate_retries=1 preserved after resume, got {updated['gate_retries']}"
        )
