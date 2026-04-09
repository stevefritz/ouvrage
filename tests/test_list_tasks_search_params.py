"""Tests for list_tasks extended search params: query, after, before, limit, sort."""

import pytest

from switchboard.db.tasks import list_tasks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_task(db, project_id, task_id, goal, created_at=None, status="ready"):
    """Insert a task and optionally back-date its created_at/updated_at."""
    from switchboard.db.connection import get_db
    task = await db.create_task(id=task_id, project_id=project_id, goal=goal)
    if created_at:
        async with get_db() as conn:
            await conn.execute(
                "UPDATE tasks SET created_at = ?, updated_at = ? WHERE id = ?",
                (created_at, created_at, task_id),
            )
            await conn.commit()
    if status != "ready":
        task = await db.update_task(task_id, status=status)
    return task


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# limit param
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# after/before params
# ---------------------------------------------------------------------------

class TestDateRangeParams:


    async def test_after_and_before_combined(self, db, sample_project):
        """after + before together form a date range window."""
        await _make_task(db, "test-project", "test-project/too-old", "Too old",
                         created_at="2020-01-01T00:00:00Z")
        await _make_task(db, "test-project", "test-project/in-range", "In range",
                         created_at="2023-06-01T00:00:00Z")
        await _make_task(db, "test-project", "test-project/too-new", "Too new",
                         created_at="2026-04-01T00:00:00Z")
        result = await list_tasks(
            project_id="test-project",
            after="2022-01-01T00:00:00Z",
            before="2024-01-01T00:00:00Z",
            limit=50,
        )
        ids = [t["id"] for t in result]
        assert "test-project/in-range" in ids
        assert "test-project/too-old" not in ids
        assert "test-project/too-new" not in ids


# ---------------------------------------------------------------------------
# query param (FTS)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FTS sanitization applied
# ---------------------------------------------------------------------------

class TestFtsSanitizationApplied:
    async def test_empty_query_returns_empty_via_sanitization(self, db, sample_project):
        """sanitize_fts_query returns None for empty → list_tasks returns [] immediately."""
        await _make_task(db, "test-project", "test-project/san1", "Sanitization test task")
        assert await list_tasks(project_id="test-project", query="") == []
        assert await list_tasks(project_id="test-project", query="  ") == []


# ---------------------------------------------------------------------------
# sort param
# ---------------------------------------------------------------------------

class TestSortParam:


    async def test_sort_relevance_without_query_falls_back_to_date(self, db, sample_project):
        """sort=relevance without query param falls back to date sort gracefully."""
        await _make_task(db, "test-project", "test-project/fb", "Fallback task")
        result = await list_tasks(project_id="test-project", sort="relevance", limit=50)
        assert isinstance(result, list)
        assert len(result) >= 1


# ---------------------------------------------------------------------------
# Combined params
# ---------------------------------------------------------------------------

class TestCombinedParams:
    async def test_query_and_project_id(self, db, sample_project):
        """query + project_id filters to project and FTS."""
        from switchboard.db.connection import get_db
        # Create second project
        await db.create_project(
            id="other-proj",
            repo="git@github.com:x/y.git",
            working_dir="/work/y",
            default_branch="main",
            test_command="pytest",
        )
        await _make_task(db, "test-project", "test-project/qp1", "Deploy kubernetes on staging")
        await _make_task(db, "other-proj", "other-proj/qp2", "Deploy kubernetes on prod")

        async with get_db() as conn:
            await conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES ('rebuild')")
            await conn.commit()

        result = await list_tasks(project_id="test-project", query="kubernetes", limit=50)
        ids = [t["id"] for t in result]
        assert "test-project/qp1" in ids
        assert "other-proj/qp2" not in ids


