"""Tests for search_weights service: CRUD, upsert semantics, and validation."""

import pytest
import ouvrage.db as db


class TestSearchWeightsSet:
    async def test_set_creates_row(self, db):
        row = await db.set_weight("task", "task-1", 1.5, reason="boosted")
        assert row["entity_type"] == "task"
        assert row["entity_id"] == "task-1"
        assert row["weight"] == 1.5
        assert row["reason"] == "boosted"
        assert row["created_at"] is not None
        assert row["updated_at"] is not None

    async def test_set_upserts_existing_row(self, db):
        await db.set_weight("task", "task-1", 1.0, reason="initial")
        updated = await db.set_weight("task", "task-1", 2.0, reason="updated")
        # Unique-per-result: only one row should exist
        all_rows = await db.list_weights("task")
        assert len(all_rows) == 1
        assert updated["weight"] == 2.0
        assert updated["reason"] == "updated"

    async def test_set_no_reason(self, db):
        row = await db.set_weight("message", "msg-99", 0.5)
        assert row["reason"] is None
        assert row["weight"] == 0.5

    async def test_set_boundary_weights(self, db):
        row_min = await db.set_weight("chunk", "chunk-1", 0.0)
        assert row_min["weight"] == 0.0
        row_max = await db.set_weight("chunk", "chunk-2", 3.0)
        assert row_max["weight"] == 3.0


class TestSearchWeightsRemove:
    async def test_remove_deletes_row(self, db):
        await db.set_weight("task", "task-del", 1.0)
        await db.remove_weight("task", "task-del")
        assert await db.get_weight("task", "task-del") is None

    async def test_remove_absent_is_noop(self, db):
        # Should not raise
        await db.remove_weight("task", "does-not-exist")


class TestSearchWeightsGet:
    async def test_get_returns_row(self, db):
        await db.set_weight("message", "m-1", 2.5)
        row = await db.get_weight("message", "m-1")
        assert row is not None
        assert row["entity_id"] == "m-1"
        assert row["weight"] == 2.5

    async def test_get_returns_none_if_absent(self, db):
        result = await db.get_weight("task", "nonexistent")
        assert result is None


class TestSearchWeightsList:
    async def test_list_returns_all(self, db):
        await db.set_weight("task", "t-1", 1.0)
        await db.set_weight("message", "m-1", 2.0)
        await db.set_weight("chunk", "c-1", 0.5)
        rows = await db.list_weights()
        assert len(rows) == 3

    async def test_list_filter_by_entity_type(self, db):
        await db.set_weight("task", "t-1", 1.0)
        await db.set_weight("task", "t-2", 1.5)
        await db.set_weight("message", "m-1", 2.0)
        task_rows = await db.list_weights("task")
        assert len(task_rows) == 2
        assert all(r["entity_type"] == "task" for r in task_rows)

    async def test_list_empty_returns_empty_list(self, db):
        rows = await db.list_weights()
        assert rows == []


class TestSearchWeightsValidation:
    async def test_weight_below_range_raises(self, db):
        with pytest.raises(ValueError, match="out of range"):
            await db.set_weight("task", "t-1", -0.1)

    async def test_weight_above_range_raises(self, db):
        with pytest.raises(ValueError, match="out of range"):
            await db.set_weight("task", "t-1", 3.1)

    async def test_invalid_entity_type_raises(self, db):
        with pytest.raises(ValueError, match="Invalid entity_type"):
            await db.set_weight("project", "p-1", 1.0)

    async def test_invalid_entity_type_empty_string(self, db):
        with pytest.raises(ValueError, match="Invalid entity_type"):
            await db.set_weight("", "e-1", 1.0)
