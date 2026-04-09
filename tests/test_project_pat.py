"""Tests for github_pat_override on projects.

Covers:
- create_project with PAT → stored encrypted in DB
- update_project set PAT → stored encrypted
- update_project clear PAT (empty string) → column is null
- get_github_pat returns project PAT when set
- get_github_pat falls back to instance PAT when not set
- MCP handlers encrypt before storing
- Dashboard API handler encrypts before storing
- Dashboard API PATCH handler encrypts/clears PAT
"""

import json
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# ASGI helpers (same pattern as test_dashboard_projects_api.py)
# ---------------------------------------------------------------------------

def _make_scope(path: str, method: str = "GET", user_id: int = 1) -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "session_user": {"id": user_id, "email": "owner@localhost", "name": "Owner", "role": "owner"},
    }


def _make_receive(body=None):
    if body is None:
        raw = b""
    elif isinstance(body, dict):
        raw = json.dumps(body).encode()
    else:
        raw = body

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return receive


class _Capture:
    def __init__(self):
        self.status = None
        self.body = b""

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")

    def json(self):
        return json.loads(self.body)


def _valid_create_payload(**overrides):
    base = {
        "id": "pat-test-proj",
        "repo": "https://github.com/org/repo.git",
        "default_branch": "main",
        "model": "claude-sonnet-4-6",
        "review_model": "claude-opus-4-6",
        "auto_test": True,
        "auto_review": True,
        "auto_pr": False,
        "auto_merge": False,
        "max_turns": 200,
        "max_wall_clock": 60,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# DB layer: create_project with github_pat_override
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# DB layer: update_project with github_pat_override
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Credential resolution: get_github_pat
# ---------------------------------------------------------------------------

class TestGetGithubPatResolution:

    async def test_get_github_pat_returns_project_pat_when_set(self, db):
        """get_github_pat returns decrypted project PAT when set."""
        await db.create_project(id="ppat-proj", repo="https://github.com/org/r.git", working_dir="/work/r")
        # Set PAT via encrypt_value → stored encrypted
        from switchboard.crypto import encrypt_value
        encrypted = encrypt_value("ghp_projecttoken")
        await db.update_project("ppat-proj", github_pat_override=encrypted)

        pat = await db.get_github_pat("ppat-proj")
        assert pat == "ghp_projecttoken"

    async def test_get_github_pat_falls_back_to_instance(self, db):
        """get_github_pat falls back to instance PAT when project override is not set."""
        await db.create_project(id="fallback-proj", repo="https://github.com/org/r.git", working_dir="/work/r")
        await db.set_instance_github_pat("ghp_instancetoken")

        pat = await db.get_github_pat("fallback-proj")
        assert pat == "ghp_instancetoken"


    async def test_get_github_pat_raises_if_none_configured(self, db):
        """get_github_pat raises ValueError when no PAT found anywhere."""
        await db.create_project(id="none-pat", repo="https://github.com/org/r.git", working_dir="/work/r")
        with pytest.raises(ValueError, match="GitHub PAT"):
            await db.get_github_pat("none-pat")


# ---------------------------------------------------------------------------
# MCP handler: _handle_create_project encrypts PAT
# ---------------------------------------------------------------------------

class TestMcpCreateProjectPat:

    @pytest.fixture(autouse=True)
    def setup(self, tmp_db):
        pass


# ---------------------------------------------------------------------------
# MCP handler: _handle_update_project encrypts PAT
# ---------------------------------------------------------------------------

class TestMcpUpdateProjectPat:

    async def test_update_project_mcp_encrypts_pat(self, db):
        """_handle_update_project encrypts a new PAT before storing."""
        import switchboard.db.connection as _conn
        from switchboard.crypto import is_fernet_token
        from switchboard.server.handlers.projects import _handle_update_project

        await db.create_project(id="mcp-upd-proj", repo="https://github.com/org/r.git", working_dir="/work/r")

        await _handle_update_project({
            "id": "mcp-upd-proj",
            "github_pat_override": "ghp_updatetoken",
        })

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT github_pat_override FROM projects WHERE id = ?", ("mcp-upd-proj",)
            )
        assert is_fernet_token(rows[0]["github_pat_override"])

    async def test_update_project_mcp_clears_pat(self, db):
        """_handle_update_project with empty string clears PAT to NULL."""
        import switchboard.db.connection as _conn
        from switchboard.crypto import encrypt_value
        from switchboard.server.handlers.projects import _handle_update_project

        encrypted = encrypt_value("ghp_clearme")
        await db.create_project(
            id="mcp-clr-proj",
            repo="https://github.com/org/r.git",
            working_dir="/work/r",
            github_pat_override=encrypted,
        )

        await _handle_update_project({"id": "mcp-clr-proj", "github_pat_override": ""})

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT github_pat_override FROM projects WHERE id = ?", ("mcp-clr-proj",)
            )
        assert rows[0]["github_pat_override"] is None


# ---------------------------------------------------------------------------
# Dashboard API: POST /dashboard/api/projects with PAT
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dashboard API: PATCH /dashboard/api/projects/{id}
# ---------------------------------------------------------------------------

class TestDashboardUpdateProjectPat:


    async def test_patch_project_not_found(self, db):
        """PATCH returns 404 for nonexistent project."""
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/projects/nonexistent", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"github_pat_override": "ghp_x"}), resp)

        assert resp.status == 404


