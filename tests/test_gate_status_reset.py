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

    async def test_retry_resets_gate_retries(self, db, sample_project):
        """gate_retries must be 0 after retry_task starts a new attempt."""
        from switchboard.dispatch.engine import retry_task

        task = await db.create_task(
            id="test-project/retry-gate-retries-reset",
            project_id="test-project",
            goal="Test retry resets gate_retries",
        )
        await db.update_task(task["id"], status="failed", gate_status="test-failed", gate_retries=2)

        with patch("switchboard.dispatch.engine.dispatch_task", AsyncMock(return_value={"status": "working"})):
            with patch("switchboard.dispatch.engine._invalidate_chain", AsyncMock()):
                await retry_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["gate_retries"] == 0, (
            f"Expected gate_retries=0 after retry, got {updated['gate_retries']}"
        )


class TestResumeResetsGateStatus:
    """resume_task must reset gate_status and gate_retries on new attempt."""

    async def test_resume_resets_gate_status(self, db, sample_project):
        """gate_status must be null after resume_task."""
        from switchboard.dispatch.engine import resume_task

        task = await db.create_task(
            id="test-project/resume-gate-reset",
            project_id="test-project",
            goal="Test resume resets gate_status",
        )
        # Use needs-review status (valid for resume); no gate_passed_at so it
        # goes through the dispatch path, not the post-gate re-trigger path.
        await db.update_task(task["id"], status="needs-review", gate_status="test-failed", gate_retries=1)

        with patch("switchboard.dispatch.engine.dispatch_task", AsyncMock(return_value={"status": "working"})):
            await resume_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["gate_status"] is None, (
            f"Expected gate_status=None after resume, got {updated['gate_status']!r}"
        )

    async def test_resume_resets_gate_retries(self, db, sample_project):
        """gate_retries must be 0 after resume_task."""
        from switchboard.dispatch.engine import resume_task

        task = await db.create_task(
            id="test-project/resume-gate-retries-reset",
            project_id="test-project",
            goal="Test resume resets gate_retries",
        )
        await db.update_task(task["id"], status="needs-review", gate_status="test-failed", gate_retries=1)

        with patch("switchboard.dispatch.engine.dispatch_task", AsyncMock(return_value={"status": "working"})):
            await resume_task(task["id"])

        updated = await db.get_task(task["id"])
        assert updated["gate_retries"] == 0, (
            f"Expected gate_retries=0 after resume, got {updated['gate_retries']}"
        )
