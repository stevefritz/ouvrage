"""Tests for FTS5 full-text search functions.

Covers:
- search_messages_fts: basic query, conversation_id filter, project_id filter, BM25 ranking
- search_tasks_fts: basic query, project_id filter, BM25 ranking
- FTS triggers: insert/update/delete keep indexes in sync
- Empty results for no match
"""

import pytest

import switchboard.db as db_module
from switchboard.db.search import search_messages_fts, search_tasks_fts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _insert_message(db, conversation_id=None, task_id=None, content="hello world", author="tester", type_="note"):
    """Insert a message via post_message (conversation) or direct insert (task)."""
    if conversation_id:
        row = await db.post_message(
            conversation_id=conversation_id,
            author=author,
            content=content,
            type=type_,
        )
        return row
    # For task messages, use direct insert since post_message only supports conversation_id
    from switchboard.db.connection import get_db
    from switchboard.db._helpers import now_iso
    async with get_db() as conn:
        cursor = await conn.execute(
            """INSERT INTO messages (conversation_id, task_id, author, type, content, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (None, task_id, author, type_, content, now_iso()),
        )
        await conn.commit()
        rows = await conn.execute_fetchall("SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,))
        return dict(rows[0]) if rows else {}


# ---------------------------------------------------------------------------
# messages_fts
# ---------------------------------------------------------------------------

class TestSearchMessagesFts:
    async def test_basic_match(self, db, sample_conversation):
        results = await search_messages_fts("redesign")
        assert len(results) > 0
        ids = [r["message_id"] for r in results]
        assert any(isinstance(id_, int) for id_ in ids)

    async def test_returns_expected_fields(self, db, sample_conversation):
        results = await search_messages_fts("redesign")
        assert len(results) > 0
        r = results[0]
        assert "message_id" in r
        assert "snippet" in r
        assert "bm25_score" in r
        assert "author" in r
        assert "type" in r
        assert "task_id" in r
        assert "conversation_id" in r
        assert "created_at" in r

    async def test_bm25_score_positive(self, db, sample_conversation):
        """bm25_score is negated so higher is better (positive for matches)."""
        results = await search_messages_fts("redesign")
        assert len(results) > 0
        for r in results:
            assert r["bm25_score"] > 0

    async def test_snippet_non_empty(self, db, sample_conversation):
        results = await search_messages_fts("redesign")
        assert len(results) > 0
        for r in results:
            assert r["snippet"]

    async def test_no_match_returns_empty(self, db, sample_conversation):
        results = await search_messages_fts("xyzzy_no_match_token")
        assert results == []

    async def test_filter_by_conversation_id(self, db, sample_project):
        conv1 = await db.create_conversation(id="conv-alpha", project="test-project", goal="alpha")
        conv2 = await db.create_conversation(id="conv-beta", project="test-project", goal="beta")

        await _insert_message(db, conversation_id="conv-alpha", content="unique_token_alpha")
        await _insert_message(db, conversation_id="conv-beta", content="unique_token_alpha")

        results = await search_messages_fts("unique_token_alpha", conversation_id="conv-alpha")
        assert len(results) == 1
        assert results[0]["conversation_id"] == "conv-alpha"

    async def test_filter_by_project_id(self, db, sample_project):
        other_project = await db.create_project(
            id="other-project",
            repo="git@github.com:acme/other.git",
            working_dir="/work/other",
            default_branch="main",
        )
        conv_main = await db.create_conversation(id="conv-main-proj", project="test-project", goal="main")
        conv_other = await db.create_conversation(id="conv-other-proj", project="other-project", goal="other")

        await _insert_message(db, conversation_id="conv-main-proj", content="project_token_xyz")
        await _insert_message(db, conversation_id="conv-other-proj", content="project_token_xyz")

        results = await search_messages_fts("project_token_xyz", project_id="test-project")
        assert len(results) == 1
        assert results[0]["conversation_id"] == "conv-main-proj"

    async def test_limit_respected(self, db, sample_project):
        conv = await db.create_conversation(id="conv-limit", project="test-project", goal="limit test")
        for i in range(5):
            await _insert_message(db, conversation_id="conv-limit", content=f"common_word_token {i}")

        results = await search_messages_fts("common_word_token", limit=3)
        assert len(results) <= 3

    async def test_ranking_by_relevance(self, db, sample_project):
        """Message with more occurrences of the query term should rank higher."""
        conv = await db.create_conversation(id="conv-rank", project="test-project", goal="ranking")
        # Insert a highly relevant message (token appears multiple times)
        await _insert_message(db, conversation_id="conv-rank",
                               content="rankterm rankterm rankterm highly relevant")
        # Insert a less relevant message (token appears once)
        await _insert_message(db, conversation_id="conv-rank",
                               content="only one rankterm here")

        results = await search_messages_fts("rankterm")
        assert len(results) >= 2
        # Higher bm25_score = more relevant; first result should have higher or equal score
        assert results[0]["bm25_score"] >= results[-1]["bm25_score"]

    async def test_update_trigger_updates_index(self, db, sample_project):
        """After updating a message's content, FTS index should reflect the change."""
        conv = await db.create_conversation(id="conv-upd", project="test-project", goal="update test")
        msg = await _insert_message(db, conversation_id="conv-upd", content="old_content_phrase")

        # Confirm old content is indexed
        results_old = await search_messages_fts("old_content_phrase")
        assert len(results_old) > 0

        # Update the message content directly
        from switchboard.db.connection import get_db
        async with get_db() as conn:
            await conn.execute(
                "UPDATE messages SET content = ? WHERE id = ?",
                ("new_content_phrase_updated", msg["id"]),
            )
            await conn.commit()

        # Old content should no longer match
        results_old_after = await search_messages_fts("old_content_phrase")
        assert all(r["message_id"] != msg["id"] for r in results_old_after)

        # New content should match
        results_new = await search_messages_fts("new_content_phrase_updated")
        assert any(r["message_id"] == msg["id"] for r in results_new)

    async def test_delete_trigger_removes_from_index(self, db, sample_project):
        """After deleting a message, it should no longer appear in FTS results."""
        conv = await db.create_conversation(id="conv-del", project="test-project", goal="delete test")
        msg = await _insert_message(db, conversation_id="conv-del", content="delete_me_unique_fts_token")

        results_before = await search_messages_fts("delete_me_unique_fts_token")
        assert any(r["message_id"] == msg["id"] for r in results_before)

        from switchboard.db.connection import get_db
        async with get_db() as conn:
            await conn.execute("DELETE FROM messages WHERE id = ?", (msg["id"],))
            await conn.commit()

        results_after = await search_messages_fts("delete_me_unique_fts_token")
        assert all(r["message_id"] != msg["id"] for r in results_after)


# ---------------------------------------------------------------------------
# tasks_fts
# ---------------------------------------------------------------------------

class TestSearchTasksFts:
    async def test_basic_match(self, db, sample_task):
        results = await search_tasks_fts("widget sorting")
        assert len(results) > 0

    async def test_returns_expected_fields(self, db, sample_task):
        results = await search_tasks_fts("widget")
        assert len(results) > 0
        r = results[0]
        assert "task_id" in r
        assert "goal" in r
        assert "bm25_score" in r
        assert "status" in r
        assert "created_at" in r

    async def test_bm25_score_positive(self, db, sample_task):
        results = await search_tasks_fts("widget")
        assert len(results) > 0
        for r in results:
            assert r["bm25_score"] > 0

    async def test_no_match_returns_empty(self, db, sample_task):
        results = await search_tasks_fts("xyzzy_no_match_token_task")
        assert results == []

    async def test_filter_by_project_id(self, db, sample_project):
        other_project = await db.create_project(
            id="other-project-tasks",
            repo="git@github.com:acme/other2.git",
            working_dir="/work/other2",
            default_branch="main",
        )
        task_main = await db.create_task(
            id="test-project/task-fts-main",
            project_id="test-project",
            goal="unique_task_goal_fts_token implementation",
        )
        task_other = await db.create_task(
            id="other-project-tasks/task-fts-other",
            project_id="other-project-tasks",
            goal="unique_task_goal_fts_token implementation",
        )

        results = await search_tasks_fts("unique_task_goal_fts_token", project_id="test-project")
        assert len(results) == 1
        assert results[0]["task_id"] == "test-project/task-fts-main"

    async def test_limit_respected(self, db, sample_project):
        for i in range(5):
            await db.create_task(
                id=f"test-project/task-limit-{i}",
                project_id="test-project",
                goal=f"limit_search_token task number {i}",
            )

        results = await search_tasks_fts("limit_search_token", limit=2)
        assert len(results) <= 2

    async def test_task_id_is_text_id(self, db, sample_task):
        """task_id in results should be the TEXT id column, not an integer rowid."""
        results = await search_tasks_fts("widget")
        assert len(results) > 0
        assert results[0]["task_id"] == sample_task["id"]

    async def test_update_trigger_updates_index(self, db, sample_project):
        """After updating a task's goal, FTS index should reflect the change."""
        task = await db.create_task(
            id="test-project/task-upd-fts",
            project_id="test-project",
            goal="old_goal_fts_phrase",
        )

        results_old = await search_tasks_fts("old_goal_fts_phrase")
        assert any(r["task_id"] == task["id"] for r in results_old)

        from switchboard.db.connection import get_db
        async with get_db() as conn:
            await conn.execute(
                "UPDATE tasks SET goal = ? WHERE id = ?",
                ("new_goal_fts_phrase_updated", task["id"]),
            )
            await conn.commit()

        results_old_after = await search_tasks_fts("old_goal_fts_phrase")
        assert all(r["task_id"] != task["id"] for r in results_old_after)

        results_new = await search_tasks_fts("new_goal_fts_phrase_updated")
        assert any(r["task_id"] == task["id"] for r in results_new)

    async def test_delete_trigger_removes_from_index(self, db, sample_project):
        """After deleting a task, it should not appear in FTS results."""
        task = await db.create_task(
            id="test-project/task-del-fts",
            project_id="test-project",
            goal="delete_task_unique_fts_token goal",
        )

        results_before = await search_tasks_fts("delete_task_unique_fts_token")
        assert any(r["task_id"] == task["id"] for r in results_before)

        from switchboard.db.connection import get_db
        async with get_db() as conn:
            # Must delete checklist items first due to FK
            await conn.execute("DELETE FROM task_checklist WHERE task_id = ?", (task["id"],))
            await conn.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
            await conn.commit()

        results_after = await search_tasks_fts("delete_task_unique_fts_token")
        assert all(r["task_id"] != task["id"] for r in results_after)
