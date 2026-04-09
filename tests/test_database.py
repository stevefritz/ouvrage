"""Schema validation and CRUD tests for the switchboard database.

Tests run against in-memory SQLite via init_db(). Never touches the
production database.
"""

import json

import pytest


# ===========================================================================
# Schema validation
# ===========================================================================


# ===========================================================================
# Project CRUD
# ===========================================================================


# ===========================================================================
# Task CRUD
# ===========================================================================


# ===========================================================================
# Message CRUD
# ===========================================================================

class TestMessageCRUD:

    async def _seed(self, db):
        await db.create_project(id="msg-proj", repo="git@x.git", working_dir="/w")
        await db.create_task(id="msg-proj/t1", project_id="msg-proj", goal="Msg test")


    async def test_cursor_pagination(self, db):
        await self._seed(db)
        await db.post_task_message(task_id="msg-proj/t1", author="a", content="msg 1")
        await db.post_task_message(task_id="msg-proj/t1", author="a", content="msg 2")
        await db.post_task_message(task_id="msg-proj/t1", author="a", content="msg 3")

        # Read all, get cursor
        result = await db.read_task_messages("msg-proj/t1")
        assert len(result["messages"]) == 3
        cursor = result["cursor"]

        # Add one more and read with cursor
        await db.post_task_message(task_id="msg-proj/t1", author="a", content="msg 4")
        result2 = await db.read_task_messages("msg-proj/t1", after=cursor)
        assert len(result2["messages"]) == 1
        assert result2["messages"][0]["content"] == "msg 4"

    async def test_pinned_message(self, db):
        await db.create_conversation(id="pin-conv", project="test", goal="Pin test")
        await db.post_message(
            conversation_id="pin-conv", author="stephen",
            content="This is the spec", type="spec", pinned=True,
        )
        pinned = await db.get_pinned("pin-conv")
        assert pinned is not None
        assert pinned["content"] == "This is the spec"


# ===========================================================================
# Conversation listing aggregates (has_pinned, pinned_title, message_count)
# ===========================================================================

class TestConversationListAggregates:
    """Tests for _list_with_aggregates via list_conversations().

    Covers has_pinned / pinned_title fields and correct message_count
    when pinned messages exist (guards against JOIN inflation).
    """


    async def test_has_pinned_true_when_pinned_message_exists(self, db):
        await db.create_conversation(id="agg-conv-2", project="agg-proj2", goal="Has pin")
        await db.post_message(
            conversation_id="agg-conv-2", author="u", content="spec",
            title="Spec title", pinned=True,
        )
        convs = await db.list_conversations(project="agg-proj2")
        assert len(convs) == 1
        assert convs[0]["has_pinned"]


# ===========================================================================
# Checklist operations
# ===========================================================================

class TestChecklistOperations:

    async def _seed(self, db):
        await db.create_project(id="cl-proj", repo="git@x.git", working_dir="/w")
        await db.create_task(id="cl-proj/t1", project_id="cl-proj", goal="CL test")


    async def test_add_item(self, db):
        await self._seed(db)
        await db.create_checklist_items("cl-proj/t1", ["Original"])
        new_item = await db.add_checklist_item("cl-proj/t1", "Added later")
        assert new_item["item"] == "Added later"

        all_items = await db.get_checklist("cl-proj/t1")
        assert len(all_items) == 2

    async def test_remove_item(self, db):
        await self._seed(db)
        items = await db.create_checklist_items("cl-proj/t1", ["Keep", "Remove"])
        removed = await db.remove_checklist_item(items[1]["id"])
        assert removed["removed"] is True

        remaining = await db.get_checklist("cl-proj/t1")
        assert len(remaining) == 1
        assert remaining[0]["item"] == "Keep"

    async def test_update_item_text(self, db):
        await self._seed(db)
        items = await db.create_checklist_items("cl-proj/t1", ["Old text"])
        updated = await db.update_checklist_item_text(items[0]["id"], "New text")
        assert updated["item"] == "New text"
