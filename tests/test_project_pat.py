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

class TestCreateProjectWithPat:

    async def test_create_project_stores_pat(self, db):
        """create_project accepts github_pat_override and stores it."""
        from switchboard.crypto import encrypt_value
        encrypted = encrypt_value("ghp_testtoken")

        proj = await db.create_project(
            id="pat-proj",
            repo="https://github.com/org/repo.git",
            working_dir="/work/repo",
            github_pat_override=encrypted,
        )
        assert proj["github_pat_override"] == encrypted

    async def test_create_project_without_pat_is_null(self, db):
        """create_project without github_pat_override stores NULL."""
        proj = await db.create_project(
            id="nopat-proj",
            repo="https://github.com/org/repo.git",
            working_dir="/work/repo",
        )
        assert proj["github_pat_override"] is None

    async def test_get_project_returns_pat(self, db):
        """get_project returns the stored github_pat_override."""
        from switchboard.crypto import encrypt_value
        encrypted = encrypt_value("ghp_readback")

        await db.create_project(
            id="getpat-proj",
            repo="https://github.com/org/repo.git",
            working_dir="/work/repo",
            github_pat_override=encrypted,
        )
        proj = await db.get_project("getpat-proj")
        assert proj["github_pat_override"] == encrypted


# ---------------------------------------------------------------------------
# DB layer: update_project with github_pat_override
# ---------------------------------------------------------------------------

class TestUpdateProjectPat:

    async def test_update_project_sets_pat(self, db):
        """update_project can set github_pat_override."""
        from switchboard.crypto import encrypt_value, is_fernet_token
        await db.create_project(id="upd-pat", repo="https://github.com/org/r.git", working_dir="/work/r")
        encrypted = encrypt_value("ghp_newtoken")
        updated = await db.update_project("upd-pat", github_pat_override=encrypted)
        assert is_fernet_token(updated["github_pat_override"])

    async def test_update_project_clears_pat_with_empty_string(self, db):
        """update_project with empty string clears github_pat_override to NULL."""
        from switchboard.crypto import encrypt_value
        encrypted = encrypt_value("ghp_clearme")
        await db.create_project(
            id="clr-pat",
            repo="https://github.com/org/r.git",
            working_dir="/work/r",
            github_pat_override=encrypted,
        )
        updated = await db.update_project("clr-pat", github_pat_override="")
        assert updated["github_pat_override"] is None

    async def test_update_project_clears_pat_with_none(self, db):
        """update_project with None clears github_pat_override."""
        from switchboard.crypto import encrypt_value
        encrypted = encrypt_value("ghp_clearme2")
        await db.create_project(
            id="clrn-pat",
            repo="https://github.com/org/r.git",
            working_dir="/work/r",
            github_pat_override=encrypted,
        )
        updated = await db.update_project("clrn-pat", github_pat_override=None)
        assert updated["github_pat_override"] is None


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

    async def test_get_github_pat_project_overrides_instance(self, db):
        """Project PAT takes priority over instance PAT."""
        await db.set_instance_github_pat("ghp_instancetoken")
        from switchboard.crypto import encrypt_value
        encrypted = encrypt_value("ghp_projectwins")
        await db.create_project(
            id="prio-proj",
            repo="https://github.com/org/r.git",
            working_dir="/work/r",
            github_pat_override=encrypted,
        )

        pat = await db.get_github_pat("prio-proj")
        assert pat == "ghp_projectwins"

    async def test_get_github_pat_raises_if_none_configured(self, db):
        """get_github_pat raises ValueError when no PAT found anywhere."""
        await db.create_project(id="none-pat", repo="https://github.com/org/r.git", working_dir="/work/r")
        with pytest.raises(ValueError, match="GitHub PAT"):
            await db.get_github_pat("none-pat")

    async def test_pat_override_encrypted_in_db(self, db):
        """github_pat_override is Fernet-encrypted in the raw DB row."""
        import switchboard.db.connection as _conn
        from switchboard.crypto import encrypt_value, is_fernet_token

        encrypted = encrypt_value("ghp_rawcheck")
        await db.create_project(
            id="rawchk-proj",
            repo="https://github.com/org/r.git",
            working_dir="/work/r",
            github_pat_override=encrypted,
        )
        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT github_pat_override FROM projects WHERE id = ?", ("rawchk-proj",)
            )
        assert is_fernet_token(rows[0]["github_pat_override"])


# ---------------------------------------------------------------------------
# MCP handler: _handle_create_project encrypts PAT
# ---------------------------------------------------------------------------

class TestMcpCreateProjectPat:

    @pytest.fixture(autouse=True)
    def setup(self, tmp_db):
        pass

    async def test_create_project_mcp_encrypts_pat(self, db):
        """_handle_create_project encrypts github_pat_override before storing."""
        import switchboard.db.connection as _conn
        from switchboard.crypto import is_fernet_token
        from switchboard.server.handlers.projects import _handle_create_project

        with patch("switchboard.server.handlers.projects.db.get_max_projects", AsyncMock(return_value=0)), \
             patch("switchboard.server.handlers.projects.WORKTREE_BASE", "/work"):
            await _handle_create_project({
                "id": "mcp-enc-proj",
                "repo": "https://github.com/org/repo.git",
                "model": "sonnet",
                "review_model": "opus",
                "auto_test": True,
                "auto_review": True,
                "auto_pr": False,
                "auto_merge": False,
                "max_turns": 100,
                "max_wall_clock": 30,
                "github_pat_override": "ghp_plaintextpat",
            })

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT github_pat_override FROM projects WHERE id = ?", ("mcp-enc-proj",)
            )
        assert rows, "Project should have been created"
        assert is_fernet_token(rows[0]["github_pat_override"]), "PAT should be Fernet-encrypted"

    async def test_create_project_mcp_no_pat_stores_null(self, db):
        """_handle_create_project with no PAT stores NULL."""
        import switchboard.db.connection as _conn
        from switchboard.server.handlers.projects import _handle_create_project

        with patch("switchboard.server.handlers.projects.db.get_max_projects", AsyncMock(return_value=0)), \
             patch("switchboard.server.handlers.projects.WORKTREE_BASE", "/work"):
            await _handle_create_project({
                "id": "mcp-nopat-proj",
                "repo": "https://github.com/org/repo.git",
                "model": "sonnet",
                "review_model": "opus",
                "auto_test": True,
                "auto_review": True,
                "auto_pr": False,
                "auto_merge": False,
                "max_turns": 100,
                "max_wall_clock": 30,
            })

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT github_pat_override FROM projects WHERE id = ?", ("mcp-nopat-proj",)
            )
        assert rows[0]["github_pat_override"] is None


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

class TestDashboardCreateProjectPat:

    async def test_dashboard_create_project_encrypts_pat(self, db):
        """POST /dashboard/api/projects encrypts github_pat_override before storing."""
        import switchboard.db.connection as _conn
        from switchboard.crypto import is_fernet_token
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()
        payload = _valid_create_payload(github_pat_override="ghp_dashboardpat")

        with patch("switchboard.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 201

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT github_pat_override FROM projects WHERE id = ?", ("pat-test-proj",)
            )
        assert is_fernet_token(rows[0]["github_pat_override"])

    async def test_dashboard_create_project_no_pat_stores_null(self, db):
        """POST /dashboard/api/projects without PAT stores NULL."""
        import switchboard.db.connection as _conn
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()
        payload = _valid_create_payload(id="nopat-dash-proj")

        with patch("switchboard.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 201

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT github_pat_override FROM projects WHERE id = ?", ("nopat-dash-proj",)
            )
        assert rows[0]["github_pat_override"] is None


# ---------------------------------------------------------------------------
# Dashboard API: PATCH /dashboard/api/projects/{id}
# ---------------------------------------------------------------------------

class TestDashboardUpdateProjectPat:

    async def test_patch_project_sets_pat(self, db):
        """PATCH /dashboard/api/projects/{id} encrypts and stores PAT."""
        import switchboard.db.connection as _conn
        from switchboard.crypto import is_fernet_token
        from switchboard.dashboard.api import handle_request

        await db.create_project(id="patch-proj", repo="https://github.com/org/r.git", working_dir="/work/r")

        scope = _make_scope("/dashboard/api/projects/patch-proj", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"github_pat_override": "ghp_patchtoken"}), resp)

        assert resp.status == 200

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT github_pat_override FROM projects WHERE id = ?", ("patch-proj",)
            )
        assert is_fernet_token(rows[0]["github_pat_override"])

    async def test_patch_project_clears_pat(self, db):
        """PATCH /dashboard/api/projects/{id} with empty string clears PAT."""
        import switchboard.db.connection as _conn
        from switchboard.crypto import encrypt_value
        from switchboard.dashboard.api import handle_request

        encrypted = encrypt_value("ghp_clearpatch")
        await db.create_project(
            id="clrpatch-proj",
            repo="https://github.com/org/r.git",
            working_dir="/work/r",
            github_pat_override=encrypted,
        )

        scope = _make_scope("/dashboard/api/projects/clrpatch-proj", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"github_pat_override": ""}), resp)

        assert resp.status == 200

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT github_pat_override FROM projects WHERE id = ?", ("clrpatch-proj",)
            )
        assert rows[0]["github_pat_override"] is None

    async def test_patch_project_decrypts_and_reencrypts_pat(self, db):
        """PATCH with a new PAT replaces the old encrypted value."""
        from switchboard.crypto import encrypt_value, is_fernet_token, decrypt_value
        from switchboard.dashboard.api import handle_request

        encrypted_old = encrypt_value("ghp_oldtoken")
        await db.create_project(
            id="reenc-proj",
            repo="https://github.com/org/r.git",
            working_dir="/work/r",
            github_pat_override=encrypted_old,
        )

        scope = _make_scope("/dashboard/api/projects/reenc-proj", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"github_pat_override": "ghp_newtoken"}), resp)

        assert resp.status == 200

        # Verify new PAT was stored and decrypts correctly
        proj = await db.get_project("reenc-proj")
        assert is_fernet_token(proj["github_pat_override"])
        assert decrypt_value(proj["github_pat_override"]) == "ghp_newtoken"

    async def test_patch_project_not_found(self, db):
        """PATCH returns 404 for nonexistent project."""
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/projects/nonexistent", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"github_pat_override": "ghp_x"}), resp)

        assert resp.status == 404

    async def test_patch_project_empty_body(self, db):
        """PATCH with no updatable fields returns 400."""
        from switchboard.dashboard.api import handle_request

        await db.create_project(id="empty-patch", repo="https://github.com/org/r.git", working_dir="/work/r")

        scope = _make_scope("/dashboard/api/projects/empty-patch", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"unknown_field": "value"}), resp)

        assert resp.status == 400

    async def test_patch_project_updates_non_pat_fields(self, db):
        """PATCH can update other project fields like test_command."""
        from switchboard.dashboard.api import handle_request

        await db.create_project(id="nonpat-patch", repo="https://github.com/org/r.git", working_dir="/work/r")

        scope = _make_scope("/dashboard/api/projects/nonpat-patch", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"test_command": "pytest -v"}), resp)

        assert resp.status == 200
        result = resp.json()
        assert result["test_command"] == "pytest -v"
