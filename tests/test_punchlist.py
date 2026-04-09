"""Tests for punchlist: CRUD, status flow, and gate integration."""

import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def sample_component(db, sample_project):
    return await db.create_component(
        id="chatbot",
        project_id="test-project",
        name="Chatbot",
    )


@pytest.fixture
async def sample_task(db, sample_project):
    task = await db.create_task(
        id="test-project/fix-chat",
        project_id="test-project",
        goal="Fix chatbot issues",
        branch="fix-chat",
    )
    return await db.update_task(task["id"], status="working")


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

class TestPunchlistCRUD:


    async def test_add_item_bad_component(self, db):
        with pytest.raises(ValueError, match="not found"):
            await db.add_punchlist_item("nonexistent", "Something broken")


    async def test_get_item_not_found(self, db):
        assert await db.get_punchlist_item(99999) is None


    async def test_list_punchlist_excludes_done_by_default(self, db, sample_component, sample_project):
        open_item = await db.add_punchlist_item("chatbot", "Open item")
        claimed_item = await db.add_punchlist_item("chatbot", "Claimed item")
        done_item = await db.add_punchlist_item("chatbot", "Done item")

        # Use separate tasks so we can resolve only done_item
        task_claimant = await db.create_task(
            id="test-project/claimant", project_id="test-project",
            goal="Claimant", branch="claimant",
        )
        task_resolver = await db.create_task(
            id="test-project/resolver", project_id="test-project",
            goal="Resolver", branch="resolver",
        )

        await db.claim_punchlist_item(claimed_item["id"], task_claimant["id"])
        await db.claim_punchlist_item(done_item["id"], task_resolver["id"])
        await db.resolve_punchlist_items_for_task(task_resolver["id"])

        items = await db.list_punchlist("chatbot")
        item_ids = {i["id"] for i in items}
        # open and claimed should appear, done should not
        assert open_item["id"] in item_ids
        assert claimed_item["id"] in item_ids
        assert done_item["id"] not in item_ids


# ---------------------------------------------------------------------------
# Status Flow
# ---------------------------------------------------------------------------

class TestPunchlistStatusFlow:

    async def test_claim_already_done_raises(self, db, sample_component, sample_task):
        item = await db.add_punchlist_item("chatbot", "Already resolved")
        await db.claim_punchlist_item(item["id"], sample_task["id"])
        await db.resolve_punchlist_items_for_task(sample_task["id"])
        with pytest.raises(ValueError, match="already done"):
            await db.claim_punchlist_item(item["id"], sample_task["id"])

    async def test_claim_nonexistent_raises(self, db):
        with pytest.raises(ValueError, match="not found"):
            await db.claim_punchlist_item(99999, "test-project/some-task")


# ---------------------------------------------------------------------------
# Gate Integration (multiple tasks / cross-task isolation)
# ---------------------------------------------------------------------------

class TestPunchlistGateIntegration:


    async def test_done_items_not_reverted(self, db, sample_component, sample_project):
        task_a = await db.create_task(
            id="test-project/task-a", project_id="test-project",
            goal="Task A", branch="branch-a",
        )
        task_b = await db.create_task(
            id="test-project/task-b", project_id="test-project",
            goal="Task B", branch="branch-b",
        )

        # task_a claims and resolves an item
        item = await db.add_punchlist_item("chatbot", "Already done")
        await db.claim_punchlist_item(item["id"], task_a["id"])
        await db.resolve_punchlist_items_for_task(task_a["id"])

        # Simulate task_a retry — should not affect already-done item
        count = await db.revert_punchlist_items_for_task(task_a["id"])
        assert count == 0
        assert (await db.get_punchlist_item(item["id"]))["status"] == "done"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

