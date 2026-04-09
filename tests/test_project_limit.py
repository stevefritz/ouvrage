"""Tests for project limit enforcement.

Covers:
- count_projects() DB function
- get_max_projects() reads env var and DB runtime override
- create_project handler succeeds when under limit
- create_project handler rejects when at limit with count/max in error message
- MAX_PROJECTS=0 means unlimited (no enforcement)
- Runtime config override (from /internal/config) takes precedence over env var
"""

from unittest.mock import AsyncMock, patch

import pytest

import switchboard.db as db
from switchboard.server.handlers.projects import _handle_create_project


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project_args(id_: str, n: int = 0) -> dict:
    """Minimal valid args for _handle_create_project."""
    return {
        "id": id_,
        "repo": "https://github.com/acme/widgets.git",
        "working_dir": f"/work/proj-{id_}",
        "model": "sonnet",
        "review_model": "opus",
        "auto_test": True,
        "auto_review": True,
        "auto_pr": False,
        "auto_merge": False,
        "max_turns": 100,
        "max_wall_clock": 60,
    }


# ---------------------------------------------------------------------------
# count_projects()
# ---------------------------------------------------------------------------

class TestCountProjects:


    async def test_counts_after_creating_projects(self, db):
        await db.create_project(
            id="p1", repo="https://github.com/acme/a.git",
            working_dir="/work/a", model="sonnet",
        )
        assert await db.count_projects() == 1

        await db.create_project(
            id="p2", repo="https://github.com/acme/b.git",
            working_dir="/work/b", model="sonnet",
        )
        assert await db.count_projects() == 2


# ---------------------------------------------------------------------------
# get_max_projects()
# ---------------------------------------------------------------------------

class TestGetMaxProjects:


    async def test_db_override_takes_precedence_over_env_var(self, db):
        await db.set_instance_config(max_projects=7)
        with patch("switchboard.db.instance_config._MAX_PROJECTS_ENV", 3):
            result = await db.get_max_projects()
        assert result == 7


# ---------------------------------------------------------------------------
# Handler enforcement — project creation
# ---------------------------------------------------------------------------

class TestCreateProjectLimitEnforcement:
    """Tests for the limit check in _handle_create_project."""

    @pytest.fixture(autouse=True)
    def mock_git(self):
        """Prevent real git/working_dir operations."""
        with patch("switchboard.server.handlers.projects.normalize_repo_url",
                   side_effect=lambda r: r), \
             patch("switchboard.server.handlers.projects.get_request_user_id",
                   return_value=None), \
             patch("switchboard.server.handlers.projects._run_project_validation",
                   new=AsyncMock(side_effect=lambda pid, proj: proj)), \
             patch("switchboard.server.handlers.projects.WORKTREE_BASE", "/work"):
            yield

    async def test_create_succeeds_when_under_limit(self, db):
        with patch("switchboard.server.handlers.projects.db.get_max_projects",
                   new=AsyncMock(return_value=3)), \
             patch("switchboard.server.handlers.projects.db.count_projects",
                   new=AsyncMock(return_value=2)):
            result = await _handle_create_project(_project_args("p1"))
        assert "error" not in result
        assert result["id"] == "p1"

    async def test_create_fails_when_at_limit(self, db):
        with patch("switchboard.server.handlers.projects.db.get_max_projects",
                   new=AsyncMock(return_value=3)), \
             patch("switchboard.server.handlers.projects.db.count_projects",
                   new=AsyncMock(return_value=3)):
            result = await _handle_create_project(_project_args("p2"))
        assert "error" in result
        assert "3/3" in result["error"]
        assert "Upgrade your plan" in result["error"]


# ---------------------------------------------------------------------------
# Runtime override integration
# ---------------------------------------------------------------------------

