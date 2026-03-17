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
    async def test_add_item_basic(self, db, sample_component):
        item = await db.add_punchlist_item("chatbot", "Button alignment is off")
        assert item["id"] is not None
        assert item["component_id"] == "chatbot"
        assert item["item"] == "Button alignment is off"
        assert item["status"] == "open"
        assert item["claimed_by"] is None
        assert item["resolved_by"] is None
        assert item["resolved_at"] is None
        assert item["created_at"] is not None

    async def test_add_item_with_author(self, db, sample_component):
        item = await db.add_punchlist_item("chatbot", "Fix error message", author="stephen")
        assert item["author"] == "stephen"

    async def test_add_item_bad_component(self, db):
        with pytest.raises(ValueError, match="not found"):
            await db.add_punchlist_item("nonexistent", "Something broken")

    async def test_get_item(self, db, sample_component):
        created = await db.add_punchlist_item("chatbot", "Fix typo")
        fetched = await db.get_punchlist_item(created["id"])
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["item"] == "Fix typo"

    async def test_get_item_not_found(self, db):
        assert await db.get_punchlist_item(99999) is None

    async def test_list_punchlist_basic(self, db, sample_component):
        await db.add_punchlist_item("chatbot", "Item one")
        await db.add_punchlist_item("chatbot", "Item two")
        items = await db.list_punchlist("chatbot")
        assert len(items) == 2
        assert items[0]["item"] == "Item one"
        assert items[1]["item"] == "Item two"

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

    async def test_list_punchlist_include_done(self, db, sample_component, sample_task):
        item = await db.add_punchlist_item("chatbot", "Done item")
        await db.claim_punchlist_item(item["id"], sample_task["id"])
        await db.resolve_punchlist_items_for_task(sample_task["id"])

        items = await db.list_punchlist("chatbot", include_done=True)
        assert len(items) == 1
        assert items[0]["status"] == "done"

    async def test_list_punchlist_empty(self, db, sample_component):
        items = await db.list_punchlist("chatbot")
        assert items == []


# ---------------------------------------------------------------------------
# Status Flow
# ---------------------------------------------------------------------------

class TestPunchlistStatusFlow:
    async def test_claim_item(self, db, sample_component, sample_task):
        item = await db.add_punchlist_item("chatbot", "Fix login bug")
        claimed = await db.claim_punchlist_item(item["id"], sample_task["id"])
        assert claimed["status"] == "claimed"
        assert claimed["claimed_by"] == sample_task["id"]

    async def test_claim_already_done_raises(self, db, sample_component, sample_task):
        item = await db.add_punchlist_item("chatbot", "Already resolved")
        await db.claim_punchlist_item(item["id"], sample_task["id"])
        await db.resolve_punchlist_items_for_task(sample_task["id"])
        with pytest.raises(ValueError, match="already done"):
            await db.claim_punchlist_item(item["id"], sample_task["id"])

    async def test_claim_nonexistent_raises(self, db):
        with pytest.raises(ValueError, match="not found"):
            await db.claim_punchlist_item(99999, "test-project/some-task")

    async def test_resolve_on_gate_pass(self, db, sample_component, sample_task):
        item1 = await db.add_punchlist_item("chatbot", "Fix A")
        item2 = await db.add_punchlist_item("chatbot", "Fix B")
        await db.claim_punchlist_item(item1["id"], sample_task["id"])
        await db.claim_punchlist_item(item2["id"], sample_task["id"])

        count = await db.resolve_punchlist_items_for_task(sample_task["id"])
        assert count == 2

        resolved1 = await db.get_punchlist_item(item1["id"])
        assert resolved1["status"] == "done"
        assert resolved1["resolved_by"] == sample_task["id"]
        assert resolved1["resolved_at"] is not None

        resolved2 = await db.get_punchlist_item(item2["id"])
        assert resolved2["status"] == "done"

    async def test_resolve_only_affects_claimed_items(self, db, sample_component, sample_task):
        open_item = await db.add_punchlist_item("chatbot", "Still open")
        claimed_item = await db.add_punchlist_item("chatbot", "Will resolve")
        await db.claim_punchlist_item(claimed_item["id"], sample_task["id"])

        count = await db.resolve_punchlist_items_for_task(sample_task["id"])
        assert count == 1

        still_open = await db.get_punchlist_item(open_item["id"])
        assert still_open["status"] == "open"

    async def test_revert_on_task_failure(self, db, sample_component, sample_task):
        item = await db.add_punchlist_item("chatbot", "Fix that crashes on retry")
        await db.claim_punchlist_item(item["id"], sample_task["id"])

        count = await db.revert_punchlist_items_for_task(sample_task["id"])
        assert count == 1

        reverted = await db.get_punchlist_item(item["id"])
        assert reverted["status"] == "open"
        assert reverted["claimed_by"] is None

    async def test_revert_only_affects_claimed_items(self, db, sample_component, sample_task):
        open_item = await db.add_punchlist_item("chatbot", "Never claimed")
        claimed_item = await db.add_punchlist_item("chatbot", "Will revert")
        await db.claim_punchlist_item(claimed_item["id"], sample_task["id"])

        count = await db.revert_punchlist_items_for_task(sample_task["id"])
        assert count == 1

        unchanged = await db.get_punchlist_item(open_item["id"])
        assert unchanged["status"] == "open"
        assert unchanged["claimed_by"] is None

    async def test_full_flow_open_claimed_done(self, db, sample_component, sample_task):
        item = await db.add_punchlist_item("chatbot", "Full flow item")
        assert (await db.get_punchlist_item(item["id"]))["status"] == "open"

        await db.claim_punchlist_item(item["id"], sample_task["id"])
        assert (await db.get_punchlist_item(item["id"]))["status"] == "claimed"

        await db.resolve_punchlist_items_for_task(sample_task["id"])
        assert (await db.get_punchlist_item(item["id"]))["status"] == "done"

    async def test_full_flow_with_revert(self, db, sample_component, sample_task):
        item = await db.add_punchlist_item("chatbot", "Reverted item")
        assert (await db.get_punchlist_item(item["id"]))["status"] == "open"

        await db.claim_punchlist_item(item["id"], sample_task["id"])
        assert (await db.get_punchlist_item(item["id"]))["status"] == "claimed"

        # Task fails/retries — revert
        await db.revert_punchlist_items_for_task(sample_task["id"])
        reverted = await db.get_punchlist_item(item["id"])
        assert reverted["status"] == "open"
        assert reverted["claimed_by"] is None


# ---------------------------------------------------------------------------
# Gate Integration (multiple tasks / cross-task isolation)
# ---------------------------------------------------------------------------

class TestPunchlistGateIntegration:
    async def test_only_this_tasks_items_resolved(self, db, sample_component, sample_project):
        task_a = await db.create_task(
            id="test-project/task-a", project_id="test-project",
            goal="Task A", branch="branch-a",
        )
        task_b = await db.create_task(
            id="test-project/task-b", project_id="test-project",
            goal="Task B", branch="branch-b",
        )

        item_a = await db.add_punchlist_item("chatbot", "Item for task A")
        item_b = await db.add_punchlist_item("chatbot", "Item for task B")

        await db.claim_punchlist_item(item_a["id"], task_a["id"])
        await db.claim_punchlist_item(item_b["id"], task_b["id"])

        # Only task A passes gate
        count = await db.resolve_punchlist_items_for_task(task_a["id"])
        assert count == 1

        assert (await db.get_punchlist_item(item_a["id"]))["status"] == "done"
        assert (await db.get_punchlist_item(item_b["id"]))["status"] == "claimed"

    async def test_only_this_tasks_items_reverted(self, db, sample_component, sample_project):
        task_a = await db.create_task(
            id="test-project/task-a", project_id="test-project",
            goal="Task A", branch="branch-a",
        )
        task_b = await db.create_task(
            id="test-project/task-b", project_id="test-project",
            goal="Task B", branch="branch-b",
        )

        item_a = await db.add_punchlist_item("chatbot", "Item A")
        item_b = await db.add_punchlist_item("chatbot", "Item B")

        await db.claim_punchlist_item(item_a["id"], task_a["id"])
        await db.claim_punchlist_item(item_b["id"], task_b["id"])

        # Only task A retries
        count = await db.revert_punchlist_items_for_task(task_a["id"])
        assert count == 1

        assert (await db.get_punchlist_item(item_a["id"]))["status"] == "open"
        assert (await db.get_punchlist_item(item_b["id"]))["status"] == "claimed"

    async def test_resolve_returns_zero_when_nothing_claimed(self, db, sample_component, sample_task):
        await db.add_punchlist_item("chatbot", "Unclaimed item")
        count = await db.resolve_punchlist_items_for_task(sample_task["id"])
        assert count == 0

    async def test_revert_returns_zero_when_nothing_claimed(self, db, sample_component, sample_task):
        await db.add_punchlist_item("chatbot", "Unclaimed item")
        count = await db.revert_punchlist_items_for_task(sample_task["id"])
        assert count == 0

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

class TestPunchlistSchema:
    async def test_table_exists(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='punchlist'"
            )
        assert len(rows) == 1

    async def test_required_columns(self, db):
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("PRAGMA table_info(punchlist)")
        col_names = {r["name"] for r in rows}
        expected = {"id", "component_id", "item", "status", "claimed_by",
                    "resolved_by", "resolved_at", "author", "created_at"}
        assert expected.issubset(col_names)
