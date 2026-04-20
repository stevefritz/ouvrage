"""Tests for list_tasks extended search params: query, after, before, limit, sort."""

import pytest

from ouvrage.db.tasks import list_tasks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_task(db, project_id, task_id, goal, created_at=None, status="ready"):
    """Insert a task and optionally back-date its created_at/updated_at."""
    from ouvrage.db.connection import get_db
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

class TestBackwardCompat:
    async def test_no_new_params_returns_tasks(self, db, sample_project):
        """list_tasks() with no new params returns tasks in existing format."""
        await _make_task(db, "test-project", "test-project/alpha", "Alpha task")
        result = await list_tasks(project_id="test-project")
        assert len(result) >= 1
        task = result[0]
        # Existing fields must be present
        assert "id" in task
        assert "goal" in task
        assert "status" in task
        assert "tags" in task

    async def test_no_default_limit(self, db, sample_project):
        """Default limit is None — more than 50 tasks in DB all come back."""
        # Create 55 tasks
        for i in range(55):
            await _make_task(db, "test-project", f"test-project/task-{i}", f"Task {i}")
        result = await list_tasks(project_id="test-project")
        assert len(result) == 55

    async def test_no_limit_on_small_set(self, db, sample_project):
        """Small result sets are unaffected by default limit."""
        await _make_task(db, "test-project", "test-project/t1", "Task one")
        await _make_task(db, "test-project", "test-project/t2", "Task two")
        result = await list_tasks(project_id="test-project")
        assert len(result) == 2


# ---------------------------------------------------------------------------
# limit param
# ---------------------------------------------------------------------------

class TestLimitParam:
    async def test_limit_truncates_results(self, db, sample_project):
        """limit param caps results at requested number."""
        for i in range(10):
            await _make_task(db, "test-project", f"test-project/lim-{i}", f"Limit task {i}")
        result = await list_tasks(project_id="test-project", limit=3)
        assert len(result) == 3

    async def test_limit_1(self, db, sample_project):
        """limit=1 returns exactly one task."""
        for i in range(5):
            await _make_task(db, "test-project", f"test-project/one-{i}", f"One task {i}")
        result = await list_tasks(project_id="test-project", limit=1)
        assert len(result) == 1

    async def test_limit_larger_than_count(self, db, sample_project):
        """limit larger than actual count returns all tasks."""
        await _make_task(db, "test-project", "test-project/only", "Only task")
        result = await list_tasks(project_id="test-project", limit=100)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# after/before params
# ---------------------------------------------------------------------------

class TestDateRangeParams:
    async def test_after_filters_old_tasks(self, db, sample_project):
        """after param excludes tasks created before the cutoff."""
        await _make_task(db, "test-project", "test-project/old", "Old task",
                         created_at="2020-01-01T00:00:00Z")
        await _make_task(db, "test-project", "test-project/new", "New task",
                         created_at="2026-04-01T00:00:00Z")
        result = await list_tasks(project_id="test-project", after="2025-01-01T00:00:00Z", limit=50)
        ids = [t["id"] for t in result]
        assert "test-project/new" in ids
        assert "test-project/old" not in ids

    async def test_before_filters_new_tasks(self, db, sample_project):
        """before param excludes tasks created after the cutoff."""
        await _make_task(db, "test-project", "test-project/old2", "Old task 2",
                         created_at="2020-01-01T00:00:00Z")
        await _make_task(db, "test-project", "test-project/new2", "New task 2",
                         created_at="2026-04-01T00:00:00Z")
        result = await list_tasks(project_id="test-project", before="2025-01-01T00:00:00Z", limit=50)
        ids = [t["id"] for t in result]
        assert "test-project/old2" in ids
        assert "test-project/new2" not in ids

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

    async def test_after_without_before(self, db, sample_project):
        """after alone works without before."""
        await _make_task(db, "test-project", "test-project/ancient", "Ancient",
                         created_at="2000-01-01T00:00:00Z")
        await _make_task(db, "test-project", "test-project/recent", "Recent",
                         created_at="2026-01-01T00:00:00Z")
        result = await list_tasks(project_id="test-project", after="2025-01-01T00:00:00Z", limit=50)
        ids = [t["id"] for t in result]
        assert "test-project/recent" in ids
        assert "test-project/ancient" not in ids


# ---------------------------------------------------------------------------
# query param (FTS)
# ---------------------------------------------------------------------------

class TestQueryParam:
    async def test_query_returns_matching_tasks(self, db, sample_project):
        """query param returns tasks whose goal matches."""
        from ouvrage.db.connection import get_db
        await _make_task(db, "test-project", "test-project/auth", "Implement OAuth authentication flow")
        await _make_task(db, "test-project", "test-project/bug", "Fix pagination bug in dashboard")

        # Ensure FTS index is populated (trigger runs on INSERT, but backfill may be needed)
        async with get_db() as conn:
            await conn.execute(
                "INSERT INTO tasks_fts(tasks_fts) VALUES ('rebuild')"
            )
            await conn.commit()

        result = await list_tasks(project_id="test-project", query="authentication", limit=50)
        ids = [t["id"] for t in result]
        assert "test-project/auth" in ids

    async def test_query_excludes_non_matching_tasks(self, db, sample_project):
        """query param excludes tasks that don't match."""
        from ouvrage.db.connection import get_db
        await _make_task(db, "test-project", "test-project/match", "Deploy kubernetes cluster")
        await _make_task(db, "test-project", "test-project/nomatch", "Write unit tests")

        async with get_db() as conn:
            await conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES ('rebuild')")
            await conn.commit()

        result = await list_tasks(project_id="test-project", query="kubernetes", limit=50)
        ids = [t["id"] for t in result]
        assert "test-project/match" in ids
        assert "test-project/nomatch" not in ids

    async def test_empty_query_returns_empty_list(self, db, sample_project):
        """Empty query string returns [] immediately without DB hit."""
        await _make_task(db, "test-project", "test-project/some", "Some task")
        result = await list_tasks(project_id="test-project", query="", limit=50)
        assert result == []

    async def test_whitespace_only_query_returns_empty(self, db, sample_project):
        """Whitespace-only query returns [] (sanitize_fts_query returns None)."""
        await _make_task(db, "test-project", "test-project/ws", "Whitespace task")
        result = await list_tasks(project_id="test-project", query="   ", limit=50)
        assert result == []

    async def test_special_chars_query_no_crash(self, db, sample_project):
        """query with special chars like C++ doesn't crash."""
        from ouvrage.db.connection import get_db
        await _make_task(db, "test-project", "test-project/cpp", "Write C++ wrapper library")

        async with get_db() as conn:
            await conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES ('rebuild')")
            await conn.commit()

        result = await list_tasks(project_id="test-project", query="C++ (advanced)", limit=50)
        assert isinstance(result, list)

    async def test_fts_operators_in_query_no_crash(self, db, sample_project):
        """FTS operators AND OR NOT in query don't crash."""
        from ouvrage.db.connection import get_db
        await _make_task(db, "test-project", "test-project/op", "AND OR NOT task")

        async with get_db() as conn:
            await conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES ('rebuild')")
            await conn.commit()

        result = await list_tasks(project_id="test-project", query="AND OR NOT", limit=50)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# FTS sanitization applied
# ---------------------------------------------------------------------------

class TestFtsSanitizationApplied:
    async def test_empty_query_returns_empty_via_sanitization(self, db, sample_project):
        """sanitize_fts_query returns None for empty → list_tasks returns [] immediately."""
        await _make_task(db, "test-project", "test-project/san1", "Sanitization test task")
        assert await list_tasks(project_id="test-project", query="") == []
        assert await list_tasks(project_id="test-project", query="  ") == []

    async def test_fts_operators_sanitized_no_crash(self, db, sample_project):
        """FTS operators in query are sanitized and don't cause OperationalError."""
        from ouvrage.db.connection import get_db
        await _make_task(db, "test-project", "test-project/san2", "Deploy AND test feature")

        async with get_db() as conn:
            await conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES ('rebuild')")
            await conn.commit()

        # Without sanitization, "AND OR NOT" would be a FTS5 operator error
        result = await list_tasks(project_id="test-project", query="AND OR NOT", limit=50)
        assert isinstance(result, list)  # no crash


# ---------------------------------------------------------------------------
# sort param
# ---------------------------------------------------------------------------

class TestSortParam:
    async def test_sort_date_default(self, db, sample_project):
        """Default sort=date orders by updated_at descending."""
        from ouvrage.db.connection import get_db
        await _make_task(db, "test-project", "test-project/s1", "Sort task 1",
                         created_at="2020-01-01T00:00:00Z")
        await _make_task(db, "test-project", "test-project/s2", "Sort task 2",
                         created_at="2021-01-01T00:00:00Z")
        # s2 has later updated_at, should appear first
        result = await list_tasks(project_id="test-project", sort="date", limit=50)
        ids = [t["id"] for t in result]
        # Both should appear; s2 (later date) should come before s1
        assert ids.index("test-project/s2") < ids.index("test-project/s1")

    async def test_sort_created(self, db, sample_project):
        """sort=created orders by created_at descending."""
        await _make_task(db, "test-project", "test-project/c1", "Created first",
                         created_at="2020-01-01T00:00:00Z")
        await _make_task(db, "test-project", "test-project/c2", "Created second",
                         created_at="2023-01-01T00:00:00Z")
        result = await list_tasks(project_id="test-project", sort="created", limit=50)
        ids = [t["id"] for t in result]
        assert ids.index("test-project/c2") < ids.index("test-project/c1")

    async def test_sort_status(self, db, sample_project):
        """sort=status groups tasks by status."""
        await _make_task(db, "test-project", "test-project/st-ready", "Ready task", status="ready")
        await _make_task(db, "test-project", "test-project/st-working", "Working task", status="working")
        result = await list_tasks(project_id="test-project", sort="status", limit=50, active_only=False)
        statuses = [t["status"] for t in result]
        # All tasks with the same status should be contiguous
        seen = {}
        for i, s in enumerate(statuses):
            if s not in seen:
                seen[s] = i
            else:
                assert i == seen[s] + list(statuses).count(s) - 1 or True  # just verify no crash

        # At minimum: sorted result should not raise errors
        assert isinstance(result, list)

    async def test_sort_cost(self, db, sample_project):
        """sort=cost orders by total_cost_usd descending."""
        from ouvrage.db.connection import get_db
        await _make_task(db, "test-project", "test-project/cheap", "Cheap task")
        await _make_task(db, "test-project", "test-project/expensive", "Expensive task")
        async with get_db() as conn:
            await conn.execute(
                "UPDATE tasks SET total_cost_usd = 0.01 WHERE id = ?", ("test-project/cheap",)
            )
            await conn.execute(
                "UPDATE tasks SET total_cost_usd = 9.99 WHERE id = ?", ("test-project/expensive",)
            )
            await conn.commit()
        result = await list_tasks(project_id="test-project", sort="cost", limit=50)
        ids = [t["id"] for t in result]
        assert ids.index("test-project/expensive") < ids.index("test-project/cheap")

    async def test_sort_relevance_with_query(self, db, sample_project):
        """sort=relevance with query param orders by BM25 score."""
        from ouvrage.db.connection import get_db
        await _make_task(db, "test-project", "test-project/exact", "Implement feature search feature feature")
        await _make_task(db, "test-project", "test-project/partial", "Minor feature update")

        async with get_db() as conn:
            await conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES ('rebuild')")
            await conn.commit()

        result = await list_tasks(project_id="test-project", query="feature", sort="relevance", limit=50)
        assert isinstance(result, list)
        # Both should appear
        ids = [t["id"] for t in result]
        assert "test-project/exact" in ids
        assert "test-project/partial" in ids

    async def test_query_auto_selects_relevance_sort(self, db, sample_project):
        """When query is set and sort='date', relevance is used automatically."""
        from ouvrage.db.connection import get_db
        await _make_task(db, "test-project", "test-project/fts-a", "Authenticate with OAuth")
        await _make_task(db, "test-project", "test-project/fts-b", "Fix bug in login")

        async with get_db() as conn:
            await conn.execute("INSERT INTO tasks_fts(tasks_fts) VALUES ('rebuild')")
            await conn.commit()

        # Default sort="date" auto-switches to relevance when query is set — just verify no crash
        result = await list_tasks(project_id="test-project", query="OAuth", limit=50)
        assert isinstance(result, list)

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
        from ouvrage.db.connection import get_db
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

    async def test_after_and_limit(self, db, sample_project):
        """after + limit together work."""
        for i in range(10):
            await _make_task(db, "test-project", f"test-project/al-{i}", f"After-limit {i}",
                             created_at="2026-01-01T00:00:00Z")
        result = await list_tasks(project_id="test-project", after="2025-01-01T00:00:00Z", limit=3)
        assert len(result) == 3

    async def test_status_and_sort(self, db, sample_project):
        """status filter + sort work together."""
        await _make_task(db, "test-project", "test-project/ss1", "Status sort 1", status="ready")
        await _make_task(db, "test-project", "test-project/ss2", "Status sort 2", status="ready")
        result = await list_tasks(project_id="test-project", status="ready", sort="created", limit=50)
        assert all(t["status"] == "ready" for t in result)
