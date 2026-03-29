"""Tests for task audit logging and chain cancellation isolation."""

import pytest
from unittest.mock import AsyncMock, patch

import switchboard.db as db


# ---------------------------------------------------------------------------
# Audit log table and CRUD
# ---------------------------------------------------------------------------

class TestAuditLogCRUD:
    """Basic audit log read/write operations."""

    async def test_write_and_read_audit_log(self, db):
        """write_audit_log creates a record, get_audit_log retrieves it."""
        project = await db.create_project(
            id="audit-proj", repo="https://github.com/x/y.git",
            working_dir="/tmp/audit", default_branch="main",
        )
        task = await db.create_task(
            id="audit-proj/task-1", project_id="audit-proj",
            goal="Test audit logging",
        )

        record = await db.write_audit_log(
            task_id="audit-proj/task-1",
            action="cancelled",
            triggered_by="cancel-api",
            source_detail="cancel_task",
            previous_status="working",
            new_status="cancelled",
        )

        assert record["task_id"] == "audit-proj/task-1"
        assert record["action"] == "cancelled"
        assert record["triggered_by"] == "cancel-api"
        assert record["source_detail"] == "cancel_task"
        assert record["previous_status"] == "working"
        assert record["new_status"] == "cancelled"
        assert record["created_at"] is not None

        # Read back
        logs = await db.get_audit_log("audit-proj/task-1")
        assert len(logs) >= 1
        # The create_task call also writes an audit log
        cancel_logs = [l for l in logs if l["action"] == "cancelled"]
        assert len(cancel_logs) == 1
        assert cancel_logs[0]["triggered_by"] == "cancel-api"

    async def test_create_task_writes_audit_log(self, db):
        """create_task automatically writes a 'created' audit log entry."""
        await db.create_project(
            id="audit-proj2", repo="https://github.com/x/y.git",
            working_dir="/tmp/audit2", default_branch="main",
        )
        await db.create_task(
            id="audit-proj2/task-1", project_id="audit-proj2",
            goal="Test task creation audit",
        )

        logs = await db.get_audit_log("audit-proj2/task-1")
        assert len(logs) == 1
        assert logs[0]["action"] == "created"
        assert logs[0]["triggered_by"] == "user"
        assert logs[0]["new_status"] == "ready"
        assert logs[0]["previous_status"] is None

    async def test_audit_log_empty_for_unknown_task(self, db):
        """get_audit_log returns empty list for nonexistent task."""
        logs = await db.get_audit_log("nonexistent/task")
        assert logs == []


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

    async def test_cancel_sibling_a_audit_log_recorded(self):
        """Cancelling sibling A writes an audit log with correct triggered_by."""
        from switchboard.dispatch.engine import cancel_task

        await self.db.update_task("chain-proj/sibling-a", status="working")
        await cancel_task("chain-proj/sibling-a")

        logs = await self.db.get_audit_log("chain-proj/sibling-a")
        cancel_logs = [l for l in logs if l["action"] == "cancelled"]
        assert len(cancel_logs) == 1
        assert cancel_logs[0]["triggered_by"] == "cancel-api"
        assert cancel_logs[0]["previous_status"] == "working"
        assert cancel_logs[0]["new_status"] == "cancelled"

    async def test_cancel_parent_does_not_cancel_children(self):
        """Cancelling the parent should NOT cancel its dependent children.

        cancel_task only cancels the specific task, not its dependents.
        Children should remain in their current state.
        """
        from switchboard.dispatch.engine import cancel_task

        await self.db.update_task("chain-proj/parent", status="working")
        await cancel_task("chain-proj/parent")

        parent = await self.db.get_task("chain-proj/parent")
        child_a = await self.db.get_task("chain-proj/sibling-a")
        child_b = await self.db.get_task("chain-proj/sibling-b")

        assert parent["status"] == "cancelled"
        assert child_a["status"] == "ready", "Child A should NOT be cancelled by parent cancellation"
        assert child_b["status"] == "ready", "Child B should NOT be cancelled by parent cancellation"


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

    async def test_cancel_chain_cancels_descendants(self):
        """cancel_chain should cancel the root and all descendants."""
        from switchboard.dispatch.engine import cancel_chain

        result = await cancel_chain("cc-proj/root")

        root = await self.db.get_task("cc-proj/root")
        child = await self.db.get_task("cc-proj/child")
        grandchild = await self.db.get_task("cc-proj/grandchild")
        unrelated = await self.db.get_task("cc-proj/unrelated")

        assert root["status"] == "cancelled"
        assert child["status"] == "cancelled"
        assert grandchild["status"] == "cancelled"
        assert unrelated["status"] == "ready", "Unrelated task should NOT be affected"

        assert set(result["cancelled"]) == {"cc-proj/root", "cc-proj/child", "cc-proj/grandchild"}

    async def test_cancel_chain_writes_audit_logs(self):
        """cancel_chain should write audit logs for each cancelled task."""
        from switchboard.dispatch.engine import cancel_chain

        await cancel_chain("cc-proj/root")

        for tid in ("cc-proj/root", "cc-proj/child", "cc-proj/grandchild"):
            logs = await self.db.get_audit_log(tid)
            chain_cancel_logs = [l for l in logs if l["action"] == "cancelled" and l["triggered_by"] == "cancel-chain"]
            assert len(chain_cancel_logs) >= 1, f"Missing cancel-chain audit log for {tid}"

    async def test_cancel_chain_skips_already_completed(self):
        """cancel_chain skips tasks that are already completed."""
        from switchboard.dispatch.engine import cancel_chain

        await self.db.update_task("cc-proj/child", status="completed")

        result = await cancel_chain("cc-proj/root")

        # child was completed, so it and its descendant should be skipped
        child = await self.db.get_task("cc-proj/child")
        assert child["status"] == "completed"
        assert "cc-proj/child" not in result["cancelled"]


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

    async def test_invalidate_chain_cancels_working_and_marks_ready_stale(self):
        """Working dependents are cancelled, ready ones get stale gate_status."""
        from switchboard.dispatch.engine import _invalidate_chain

        await _invalidate_chain("inv-proj/parent")

        working = await self.db.get_task("inv-proj/working-child")
        ready = await self.db.get_task("inv-proj/ready-child")

        assert working["status"] == "cancelled"
        assert ready["status"] == "ready"  # status unchanged
        assert ready["gate_status"] == "stale"

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

    async def test_reopen_task_audit(self):
        """reopen_task writes audit with triggered_by=user."""
        from switchboard.dispatch.engine import reopen_task

        task = await self.db.create_task(
            id="trig-proj/reopen-test", project_id="trig-proj",
            goal="Test reopen audit",
        )
        await self.db.update_task("trig-proj/reopen-test", status="completed")

        await reopen_task("trig-proj/reopen-test")

        logs = await self.db.get_audit_log("trig-proj/reopen-test")
        reopen_logs = [l for l in logs if l["action"] == "reopened"]
        assert len(reopen_logs) == 1
        assert reopen_logs[0]["triggered_by"] == "user"
        assert reopen_logs[0]["previous_status"] == "completed"
        assert reopen_logs[0]["new_status"] == "reopened"

    async def test_skip_gate_audit(self):
        """skip_gate writes audit with triggered_by=user."""
        from switchboard.dispatch.engine import skip_gate

        task = await self.db.create_task(
            id="trig-proj/gate-test", project_id="trig-proj",
            goal="Test skip gate audit",
        )
        await self.db.update_task("trig-proj/gate-test", status="completed")

        await skip_gate("trig-proj/gate-test")

        logs = await self.db.get_audit_log("trig-proj/gate-test")
        gate_logs = [l for l in logs if l["action"] == "gate_passed"]
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

        with patch("switchboard.dispatch.engine.cleanup_worktree", new_callable=AsyncMock):
            with patch("switchboard.dispatch.engine.archive_task_logs", new_callable=AsyncMock):
                await close_task("trig-proj/close-test", cleanup=False)

        logs = await self.db.get_audit_log("trig-proj/close-test")
        close_logs = [l for l in logs if l["action"] == "closed"]
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

    async def test_cancelling_portal_pages_does_not_touch_portal_ui(self):
        """Cancelling portal-pages (old batch) must NOT cancel portal-ui (new chain)."""
        from switchboard.dispatch.engine import cancel_task

        # portal-pages is already cancelled, but let's verify
        # that cancel_chain on it doesn't cross-contaminate
        from switchboard.dispatch.engine import cancel_chain
        await self.db.update_task("fp/portal-pages", status="ready")  # un-cancel for test
        await cancel_chain("fp/portal-pages")

        portal_ui = await self.db.get_task("fp/portal-ui")
        assert portal_ui["status"] == "working", \
            "portal-ui should NOT be affected by cancelling portal-pages"

    async def test_cancelling_siblings_does_not_affect_different_chain(self):
        """Cancelling all old-batch tasks should NOT touch the new chain."""
        from switchboard.dispatch.engine import cancel_task

        # Cancel each old batch task individually
        for tid in ("fp/portal-pages", "fp/landing-page"):
            task = await self.db.get_task(tid)
            if task["status"] != "cancelled":
                await self.db.update_task(tid, status="working")
                await cancel_task(tid)

        # Verify new chain is untouched
        fe = await self.db.get_task("fp/fe-foundation")
        portal = await self.db.get_task("fp/portal-ui")
        assert fe["status"] == "working"
        assert portal["status"] == "working"
