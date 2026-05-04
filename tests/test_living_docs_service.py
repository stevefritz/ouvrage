"""Unit tests for ouvrage.services.living_docs."""

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import ouvrage.db.reference_docs as db_reference_docs
import ouvrage.services.living_docs as svc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def project(db):
    return await db.create_project(
        id="ld-test-project",
        repo="https://github.com/acme/repo.git",
        working_dir="/work/repo",
        default_branch="main",
        test_command="pytest",
        env_overrides={},
        max_turns=50,
        max_wall_clock=30,
        model="sonnet",
    )


@pytest.fixture
async def task_with_worktree(db, project, tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task = await db.create_task(
        id="ld-test-project/test-task",
        project_id="ld-test-project",
        goal="Test task",
        branch="test-branch",
    )
    task = await db.update_task(task["id"], worktree_path=str(worktree))
    return task, worktree


@pytest.fixture
async def config(db, project):
    return await db_reference_docs.upsert_config(
        project_id="ld-test-project",
        slug="architecture",
        title="Architecture Overview",
        brief="High-level system architecture doc.",
    )


@pytest.fixture
def docs_root(tmp_path, monkeypatch):
    """Redirect LOCAL_DOCS_ROOT into tmp_path for test isolation."""
    root = tmp_path / "reference_docs"
    monkeypatch.setattr(svc, "LOCAL_DOCS_ROOT", root)
    return root


# ---------------------------------------------------------------------------
# set_config validation
# ---------------------------------------------------------------------------

class TestSetConfigValidation:
    async def test_slug_regex_rejection(self, db, project):
        with pytest.raises(ValueError, match="Invalid slug"):
            await svc.set_config(
                project_id="ld-test-project",
                slug="UPPERCASE",
                title="Title",
                brief="Brief",
            )

    async def test_slug_with_leading_hyphen_rejected(self, db, project):
        with pytest.raises(ValueError, match="Invalid slug"):
            await svc.set_config(
                project_id="ld-test-project",
                slug="-invalid",
                title="Title",
                brief="Brief",
            )

    async def test_project_missing(self, db):
        with pytest.raises(ValueError, match="Project 'no-such-project' not found"):
            await svc.set_config(
                project_id="no-such-project",
                slug="valid-slug",
                title="Title",
                brief="Brief",
            )

    async def test_valid_slug_creates_config(self, db, project):
        result = await svc.set_config(
            project_id="ld-test-project",
            slug="my-doc",
            title="My Doc",
            brief="A brief description",
        )
        assert result["slug"] == "my-doc"
        assert result["project_id"] == "ld-test-project"


# ---------------------------------------------------------------------------
# add_version
# ---------------------------------------------------------------------------

class TestAddVersion:
    @pytest.fixture(autouse=True)
    def patch_create_task(self):
        """Prevent index_doc_file from actually running during tests."""
        def _close_coro(coro):
            if asyncio.iscoroutine(coro):
                coro.close()
            f = asyncio.get_event_loop().create_future()
            f.set_result(None)
            return f

        with patch("asyncio.create_task", side_effect=_close_coro) as mock_ct:
            self.create_task_mock = mock_ct
            yield mock_ct

    async def test_happy_path(self, db, task_with_worktree, config, docs_root):
        task, worktree = task_with_worktree
        source = worktree / "architecture.md"
        source.write_text(
            "# Architecture Overview\n\n"
            "## Components\n\nThis is the components section.\n" * 20
        )

        result = await svc.add_version(
            task_id=task["id"],
            slug="architecture",
            source_path=str(source),
        )

        assert result["embedded"] == "queued"
        assert result["chunkable"] is True
        assert result["file_id"]

        # File should be at target location
        target = docs_root / "ld-test-project" / "architecture.md"
        assert target.exists()
        assert "Architecture Overview" in target.read_text()

        # asyncio.create_task should have been called once (for index_doc_file)
        self.create_task_mock.assert_called_once()

    async def test_idempotent(self, db, task_with_worktree, config, docs_root):
        task, worktree = task_with_worktree
        source = worktree / "architecture.md"
        content = (
            "# Architecture Overview\n\n"
            "## Section A\n\nContent A.\n" * 20
        )
        source.write_text(content)

        result1 = await svc.add_version(
            task_id=task["id"],
            slug="architecture",
            source_path=str(source),
        )

        source.write_text(content + "\n\n## New Section\n\nNew content.\n" * 10)
        result2 = await svc.add_version(
            task_id=task["id"],
            slug="architecture",
            source_path=str(source),
        )

        # Same file_id — upserted, not duplicated
        assert result1["file_id"] == result2["file_id"]
        # Target file updated with new content
        target = docs_root / "ld-test-project" / "architecture.md"
        assert "New Section" in target.read_text()

    async def test_path_traversal_rejected(self, db, task_with_worktree, config, docs_root):
        task, worktree = task_with_worktree
        # Create a file outside the worktree
        outside = worktree.parent / "outside.md"
        outside.write_text("# Sensitive\n\n## Data\n\nSensitive content.\n" * 20)

        with pytest.raises(ValueError, match="within the worktree"):
            await svc.add_version(
                task_id=task["id"],
                slug="architecture",
                source_path=str(outside),
            )

    async def test_non_md_extension_rejected(self, db, task_with_worktree, config, docs_root):
        task, worktree = task_with_worktree
        source = worktree / "architecture.txt"
        source.write_text("Some content")

        with pytest.raises(ValueError, match=r"\.md file"):
            await svc.add_version(
                task_id=task["id"],
                slug="architecture",
                source_path=str(source),
            )

    async def test_config_not_found(self, db, task_with_worktree, docs_root):
        task, worktree = task_with_worktree
        source = worktree / "unknown.md"
        source.write_text("# Unknown\n\n## Section\n\nContent\n" * 20)

        with pytest.raises(ValueError, match="not found in project"):
            await svc.add_version(
                task_id=task["id"],
                slug="unknown-slug",
                source_path=str(source),
            )

    async def test_unchunkable_doc_warns_not_raises(self, db, task_with_worktree, config, docs_root):
        task, worktree = task_with_worktree
        # Short content with no headers — unchunkable
        source = worktree / "architecture.md"
        source.write_text("Too short to chunk.")

        with patch("ouvrage.db.post_task_message", new_callable=AsyncMock) as mock_post:
            result = await svc.add_version(
                task_id=task["id"],
                slug="architecture",
                source_path=str(source),
            )

        assert result["chunkable"] is False
        mock_post.assert_called_once()
        # Verify the warning mentions chunking
        call_args_str = str(mock_post.call_args)
        assert "chunk" in call_args_str.lower()


# ---------------------------------------------------------------------------
# delete_config cascade
# ---------------------------------------------------------------------------

class TestDeleteConfig:
    @pytest.fixture(autouse=True)
    def patch_create_task(self):
        def _close_coro(coro):
            if asyncio.iscoroutine(coro):
                coro.close()
            f = asyncio.get_event_loop().create_future()
            f.set_result(None)
            return f

        with patch("asyncio.create_task", side_effect=_close_coro):
            yield

    async def test_cascade(self, db, task_with_worktree, config, docs_root):
        task, worktree = task_with_worktree
        source = worktree / "architecture.md"
        source.write_text("# Architecture\n\n## Section\n\nContent\n" * 20)

        # Create a version first
        result = await svc.add_version(
            task_id=task["id"],
            slug="architecture",
            source_path=str(source),
        )
        file_id = result["file_id"]

        # Verify file row exists
        file_row = await db.get_file(file_id)
        assert file_row is not None

        # Verify local cache exists
        cache = docs_root / "ld-test-project" / "architecture.md"
        assert cache.exists()

        # Delete config
        await svc.delete_config("ld-test-project", "architecture")

        # Files row gone
        file_row_after = await db.get_file(file_id)
        assert file_row_after is None

        # Local cache gone
        assert not cache.exists()

        # Config row gone
        config_after = await db_reference_docs.get_config("ld-test-project", "architecture")
        assert config_after is None

    async def test_silent_if_missing(self, db, project, docs_root):
        # Should not raise when config doesn't exist
        await svc.delete_config("ld-test-project", "nonexistent-slug")


# ---------------------------------------------------------------------------
# get_local_copy
# ---------------------------------------------------------------------------

class TestGetLocalCopy:
    async def test_returns_content_when_present(self, db, project, docs_root):
        path = docs_root / "ld-test-project" / "my-doc.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Hello\n\nContent here.")

        result = await svc.get_local_copy("ld-test-project", "my-doc")
        assert result == "# Hello\n\nContent here."

    async def test_returns_none_when_missing(self, db, project, docs_root):
        result = await svc.get_local_copy("ld-test-project", "no-such-doc")
        assert result is None
