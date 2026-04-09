"""Tests for task audit logging and chain cancellation isolation."""

import pytest
from unittest.mock import AsyncMock, patch

import switchboard.db as db


# ---------------------------------------------------------------------------
# Audit log table and CRUD
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Chain cancellation isolation — siblings should NOT be affected
# ---------------------------------------------------------------------------

class TestChainCancellationIsolation:
    """Verify that cancelling a task does not affect siblings or unrelated tasks."""

    @pytest.fixture(autouse=True)
    async def setup_tasks(self, db):
        """Create a parent with two children (siblings) sharing the same depends_on."""
        self.db = db
        await db.create_project(
            id="chain-proj", repo="https://github.com/x/y.git",
            working_dir="/tmp/chain", default_branch="main",
            test_command="pytest",
        )

        # Parent task
        self.parent = await db.create_task(
            id="chain-proj/parent", project_id="chain-proj",
            goal="Parent task",
        )

        # Sibling A — depends on parent
        self.sibling_a = await db.create_task(
            id="chain-proj/sibling-a", project_id="chain-proj",
            goal="Sibling A", depends_on="chain-proj/parent",
        )

        # Sibling B — depends on same parent
        self.sibling_b = await db.create_task(
            id="chain-proj/sibling-b", project_id="chain-proj",
            goal="Sibling B", depends_on="chain-proj/parent",
        )

    async def test_cancel_sibling_a_does_not_affect_sibling_b(self):
        """Cancelling sibling A should NOT cancel sibling B."""
        from switchboard.dispatch.engine import cancel_task

        # Put sibling A into working status so it can be cancelled
        await self.db.update_task("chain-proj/sibling-a", status="working")

        await cancel_task("chain-proj/sibling-a")

        task_a = await self.db.get_task("chain-proj/sibling-a")
        task_b = await self.db.get_task("chain-proj/sibling-b")

        assert task_a["status"] == "cancelled"
        assert task_b["status"] == "ready", "Sibling B should NOT be cancelled when sibling A is cancelled"


# ---------------------------------------------------------------------------
# cancel_chain — recursive cancellation should only go DOWN the chain
# ---------------------------------------------------------------------------

class TestCancelChain:
    """cancel_chain cancels dependents but not siblings or ancestors."""

    @pytest.fixture(autouse=True)
    async def setup_chain(self, db):
        self.db = db
        await db.create_project(
            id="cc-proj", repo="https://github.com/x/y.git",
            working_dir="/tmp/cc", default_branch="main",
        )

        # Chain: root → child → grandchild
        self.root = await db.create_task(
            id="cc-proj/root", project_id="cc-proj", goal="Root",
        )
        self.child = await db.create_task(
            id="cc-proj/child", project_id="cc-proj", goal="Child",
            depends_on="cc-proj/root",
        )
        self.grandchild = await db.create_task(
            id="cc-proj/grandchild", project_id="cc-proj", goal="Grandchild",
            depends_on="cc-proj/child",
        )
        # Unrelated task in same project
        self.unrelated = await db.create_task(
            id="cc-proj/unrelated", project_id="cc-proj", goal="Unrelated",
        )


# ---------------------------------------------------------------------------
# _invalidate_chain — should cancel working dependents and mark others stale
# ---------------------------------------------------------------------------

class TestInvalidateChain:
    """_invalidate_chain cancels working dependents, marks others stale."""

    @pytest.fixture(autouse=True)
    async def setup_chain(self, db):
        self.db = db
        await db.create_project(
            id="inv-proj", repo="https://github.com/x/y.git",
            working_dir="/tmp/inv", default_branch="main",
        )

        self.parent = await db.create_task(
            id="inv-proj/parent", project_id="inv-proj", goal="Parent",
        )
        self.working_child = await db.create_task(
            id="inv-proj/working-child", project_id="inv-proj",
            goal="Working child", depends_on="inv-proj/parent",
        )
        await self.db.update_task("inv-proj/working-child", status="working")

        self.ready_child = await db.create_task(
            id="inv-proj/ready-child", project_id="inv-proj",
            goal="Ready child", depends_on="inv-proj/parent",
        )


    async def test_invalidate_chain_writes_audit_logs(self):
        """_invalidate_chain writes audit logs with triggered_by=chain-invalidation."""
        from switchboard.dispatch.engine import _invalidate_chain

        await _invalidate_chain("inv-proj/parent")

        # Working child should have a chain-invalidation audit entry
        logs = await self.db.get_audit_log("inv-proj/working-child")
        inv_logs = [l for l in logs if l["triggered_by"] == "chain-invalidation"]
        assert len(inv_logs) >= 1

        # Ready child should have a stale audit entry
        logs = await self.db.get_audit_log("inv-proj/ready-child")
        stale_logs = [l for l in logs if l["action"] == "stale"]
        assert len(stale_logs) == 1
        assert stale_logs[0]["triggered_by"] == "chain-invalidation"


# ---------------------------------------------------------------------------
# Audit log records correct triggered_by for various operations
# ---------------------------------------------------------------------------

class TestAuditTriggeredBy:
    """Verify triggered_by values are correct for different operations."""

    @pytest.fixture(autouse=True)
    async def setup(self, db):
        self.db = db
        await db.create_project(
            id="trig-proj", repo="https://github.com/x/y.git",
            working_dir="/tmp/trig", default_branch="main",
            test_command="pytest",
        )


    async def test_skip_gate_audit(self):
        """skip_gate writes audit with triggered_by=user."""
        from switchboard.dispatch.engine import skip_gate

        task = await self.db.create_task(
            id="trig-proj/gate-test", project_id="trig-proj",
            goal="Test skip gate audit",
        )
        # Put in pending-validation (maps to "validating" effective state)
        await self.db.update_task("trig-proj/gate-test", status="pending-validation")

        with patch("switchboard.dispatch.lifecycle._skip_gate_dispatch_dependents", new_callable=AsyncMock):
            await skip_gate("trig-proj/gate-test")

        logs = await self.db.get_audit_log("trig-proj/gate-test")
        gate_logs = [l for l in logs if l["action"] == "skip_gate"]
        assert len(gate_logs) == 1
        assert gate_logs[0]["triggered_by"] == "user"
        assert "skip_gate" in gate_logs[0]["source_detail"]

    async def test_close_task_audit(self):
        """close_task writes audit with triggered_by=user."""
        from switchboard.dispatch.engine import close_task

        task = await self.db.create_task(
            id="trig-proj/close-test", project_id="trig-proj",
            goal="Test close audit",
        )
        await self.db.update_task("trig-proj/close-test", status="needs-review")

        with patch("switchboard.dispatch.lifecycle._close_archive_and_cleanup", new_callable=AsyncMock):
            await close_task("trig-proj/close-test")

        logs = await self.db.get_audit_log("trig-proj/close-test")
        close_logs = [l for l in logs if l["action"] == "close"]
        assert len(close_logs) == 1
        assert close_logs[0]["triggered_by"] == "user"
        assert close_logs[0]["previous_status"] == "needs-review"
        assert close_logs[0]["new_status"] == "completed"


# ---------------------------------------------------------------------------
# Cross-chain isolation — the portal-ui bug scenario
# ---------------------------------------------------------------------------

class TestCrossChainIsolation:
    """Reproduce the portal-ui crossfire scenario and verify isolation."""

    @pytest.fixture(autouse=True)
    async def setup_scenario(self, db):
        """Create the exact topology from the bug report.

        test-hardening (parent of both batches)
        ├── portal-pages (old batch, cancelled)
        ├── landing-page (old batch, cancelled)
        ├── admin-panel (old batch, completed)
        └── fe-foundation (new chain root)
             └── portal-ui (the victim)
        """
        self.db = db
        await db.create_project(
            id="fp", repo="https://github.com/x/y.git",
            working_dir="/tmp/fp", default_branch="main",
        )

        await db.create_task(id="fp/test-hardening", project_id="fp", goal="Test hardening")
        await db.update_task("fp/test-hardening", status="completed",
                             gate_status="passed", gate_passed_at=db.now_iso())

        await db.create_task(id="fp/portal-pages", project_id="fp",
                             goal="Portal pages", depends_on="fp/test-hardening")
        await db.update_task("fp/portal-pages", status="cancelled")

        await db.create_task(id="fp/landing-page", project_id="fp",
                             goal="Landing page", depends_on="fp/test-hardening")
        await db.update_task("fp/landing-page", status="cancelled")

        await db.create_task(id="fp/admin-panel", project_id="fp",
                             goal="Admin panel", depends_on="fp/test-hardening")
        await db.update_task("fp/admin-panel", status="completed")

        await db.create_task(id="fp/fe-foundation", project_id="fp",
                             goal="FE foundation", depends_on="fp/test-hardening")
        await db.update_task("fp/fe-foundation", status="working")

        await db.create_task(id="fp/portal-ui", project_id="fp",
                             goal="Portal UI", depends_on="fp/fe-foundation")
        await db.update_task("fp/portal-ui", status="working")


