"""Tests for MCP file handler security: add_task_file and add_project_file worktree validation."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

import ouvrage.db as db
from ouvrage.server.context import set_request_context


def _set_worker_context():
    set_request_context(user_id=None, is_token_auth=False, is_worker=True)


def _set_non_worker_context():
    set_request_context(user_id=1, is_token_auth=True, is_worker=False)


class TestAddProjectFileWorktreeValidation:
    """add_project_file must reject source paths outside the task's worktree."""

    @pytest.fixture(autouse=True)
    def setup_uploads(self, tmp_path):
        uploads_dir = str(tmp_path / "uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        with patch("ouvrage.server.handlers.files_handler._uploads_dir",
                   return_value=Path(uploads_dir)):
            yield uploads_dir

    async def test_rejects_path_outside_worktree(self, db, sample_project, tmp_path):
        """Worker cannot upload /etc/passwd or other paths outside worktree."""
        from ouvrage.server.handlers.files_handler import _handle_add_project_file

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        task = await db.create_task(
            id="test-project/sec-task",
            project_id="test-project",
            goal="Security test",
        )
        await db.update_task(task["id"], worktree_path=str(worktree))

        # Create a file outside the worktree
        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("sensitive data")

        _set_worker_context()
        with pytest.raises(ValueError, match="must be within the worktree"):
            await _handle_add_project_file({
                "project_id": "test-project",
                "task_id": "test-project/sec-task",
                "source_path": str(outside_file),
            })

    async def test_rejects_symlink_escape(self, db, sample_project, tmp_path):
        """Symlinks that escape the worktree are rejected after resolve()."""
        from ouvrage.server.handlers.files_handler import _handle_add_project_file

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("sensitive")

        # Symlink inside worktree pointing outside
        symlink = worktree / "escape.txt"
        symlink.symlink_to(outside_file)

        task = await db.create_task(
            id="test-project/symlink-task",
            project_id="test-project",
            goal="Symlink test",
        )
        await db.update_task(task["id"], worktree_path=str(worktree))

        _set_worker_context()
        with pytest.raises(ValueError, match="must be within the worktree"):
            await _handle_add_project_file({
                "project_id": "test-project",
                "task_id": "test-project/symlink-task",
                "source_path": str(symlink),
            })

    async def test_accepts_path_inside_worktree(self, db, sample_project, tmp_path):
        """Worker can upload files that genuinely live inside the worktree."""
        from ouvrage.server.handlers.files_handler import _handle_add_project_file

        worktree = tmp_path / "worktree"
        worktree.mkdir()

        report = worktree / "report.txt"
        report.write_text("analysis results")

        task = await db.create_task(
            id="test-project/valid-task",
            project_id="test-project",
            goal="Valid upload test",
        )
        await db.update_task(task["id"], worktree_path=str(worktree))

        _set_worker_context()
        result = await _handle_add_project_file({
            "project_id": "test-project",
            "task_id": "test-project/valid-task",
            "source_path": str(report),
        })

        assert result["filename"] == "report.txt"
        assert result["project_id"] == "test-project"

    async def test_worker_requires_task_id(self, db, sample_project, tmp_path):
        """Worker call without task_id is rejected."""
        from ouvrage.server.handlers.files_handler import _handle_add_project_file

        _set_worker_context()
        with pytest.raises(ValueError, match="task_id is required"):
            await _handle_add_project_file({
                "project_id": "test-project",
                "source_path": "/some/file.txt",
            })

    async def test_non_worker_rejected(self, db, sample_project):
        """add_project_file is only available on the worker endpoint."""
        from ouvrage.server.handlers.files_handler import _handle_add_project_file

        _set_non_worker_context()
        with pytest.raises(ValueError, match="only available on the worker endpoint"):
            await _handle_add_project_file({
                "project_id": "test-project",
                "task_id": "test-project/some-task",
                "source_path": "/some/file.txt",
            })

    async def test_rejects_task_with_no_worktree(self, db, sample_project, tmp_path):
        """If the task has no worktree_path set, reject rather than skip validation."""
        from ouvrage.server.handlers.files_handler import _handle_add_project_file

        task = await db.create_task(
            id="test-project/no-worktree-task",
            project_id="test-project",
            goal="No worktree",
        )
        # worktree_path defaults to None in create_task

        _set_worker_context()
        with pytest.raises(ValueError, match="no worktree_path"):
            await _handle_add_project_file({
                "project_id": "test-project",
                "task_id": "test-project/no-worktree-task",
                "source_path": "/some/file.txt",
            })
