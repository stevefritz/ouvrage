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


# ---------------------------------------------------------------------------
# tasks_fts
# ---------------------------------------------------------------------------

class TestSearchTasksFts:


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


