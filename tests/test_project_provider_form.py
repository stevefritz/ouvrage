"""Tests for provider/credential_override fields in project create/update via dashboard API.

Covers:
- POST /dashboard/api/projects with provider + credential_override stored and encrypted
- POST /dashboard/api/projects with no credential — succeeds (non-blocking)
- PATCH /dashboard/api/projects/{id} with provider + credential_override stored
- PATCH /dashboard/api/projects/{id} clears credential_override with empty string
"""

import json
from unittest.mock import AsyncMock, patch

import pytest


# ── ASGI test helpers ─────────────────────────────────────────────────────────

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
        "id": "provider-test-proj",
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


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestCreateProjectNonBlocking:
    """Project creation succeeds without any credential configured."""

    async def test_create_project_no_credential_succeeds(self, db):
        """POST /dashboard/api/projects succeeds even with no PAT or credential configured."""
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()

        with patch("ouvrage.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(_valid_create_payload()), resp)

        # Should succeed — no longer blocked by PAT check
        assert resp.status == 201
        data = resp.json()
        assert data["id"] == "provider-test-proj"


class TestCreateProjectWithProvider:
    """POST /dashboard/api/projects stores provider and credential_override."""

    async def test_create_project_stores_provider(self, db):
        """provider field is stored in the database."""
        from ouvrage.dashboard.api import handle_request
        import ouvrage.db as sw_db

        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()
        payload = _valid_create_payload(provider="gitlab")

        with patch("ouvrage.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 201
        project = await sw_db.get_project("provider-test-proj")
        assert project["provider"] == "gitlab"

    async def test_create_project_stores_credential_override_encrypted(self, db):
        """credential_override is encrypted before storage."""
        import ouvrage.db.connection as _conn
        from ouvrage.crypto import is_fernet_token
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()
        payload = _valid_create_payload(
            id="cred-proj",
            provider="gitlab",
            credential_override="glpat-plaintexttoken",
        )

        with patch("ouvrage.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 201

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT credential_override FROM projects WHERE id = ?", ("cred-proj",)
            )
        assert rows, "Project should have been created"
        assert is_fernet_token(rows[0]["credential_override"]), "credential_override should be Fernet-encrypted"

    async def test_create_project_no_credential_stores_null(self, db):
        """No credential_override → NULL stored in DB."""
        import ouvrage.db.connection as _conn
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()
        payload = _valid_create_payload(id="nocred-proj", provider="github")

        with patch("ouvrage.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 201

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT credential_override FROM projects WHERE id = ?", ("nocred-proj",)
            )
        assert rows[0]["credential_override"] is None

    async def test_create_project_no_provider_stores_null(self, db):
        """No provider field → NULL stored."""
        import ouvrage.db.connection as _conn
        from ouvrage.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/projects", method="POST")
        resp = _Capture()

        with patch("ouvrage.dashboard.api._WORKTREE_BASE", "/work"):
            await handle_request(scope, _make_receive(_valid_create_payload()), resp)

        assert resp.status == 201

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT provider FROM projects WHERE id = ?", ("provider-test-proj",)
            )
        assert rows[0]["provider"] is None


class TestUpdateProjectWithProvider:
    """PATCH /dashboard/api/projects/{id} stores provider and credential_override."""

    async def test_patch_project_sets_provider(self, db):
        """PATCH with provider sets it in the DB."""
        import ouvrage.db.connection as _conn
        from ouvrage.dashboard.api import handle_request

        await db.create_project(id="patch-prov-proj", repo="https://github.com/org/r.git", working_dir="/work/r")

        scope = _make_scope("/dashboard/api/projects/patch-prov-proj", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"provider": "gitlab"}), resp)

        assert resp.status == 200

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT provider FROM projects WHERE id = ?", ("patch-prov-proj",)
            )
        assert rows[0]["provider"] == "gitlab"

    async def test_patch_project_sets_credential_override_encrypted(self, db):
        """PATCH with credential_override encrypts and stores it."""
        import ouvrage.db.connection as _conn
        from ouvrage.crypto import is_fernet_token
        from ouvrage.dashboard.api import handle_request

        await db.create_project(id="patch-cred-proj", repo="https://github.com/org/r.git", working_dir="/work/r")

        scope = _make_scope("/dashboard/api/projects/patch-cred-proj", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"credential_override": "glpat-newtesttoken"}), resp)

        assert resp.status == 200

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT credential_override FROM projects WHERE id = ?", ("patch-cred-proj",)
            )
        assert is_fernet_token(rows[0]["credential_override"])

    async def test_patch_project_clears_credential_override(self, db):
        """PATCH with empty string clears credential_override (sets to NULL)."""
        import ouvrage.db.connection as _conn
        from ouvrage.crypto import encrypt_value
        from ouvrage.dashboard.api import handle_request

        encrypted = encrypt_value("glpat-existingtoken")
        await db.create_project(
            id="clr-cred-proj",
            repo="https://github.com/org/r.git",
            working_dir="/work/r",
            credential_override=encrypted,
        )

        scope = _make_scope("/dashboard/api/projects/clr-cred-proj", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"credential_override": ""}), resp)

        assert resp.status == 200

        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT credential_override FROM projects WHERE id = ?", ("clr-cred-proj",)
            )
        assert rows[0]["credential_override"] is None
