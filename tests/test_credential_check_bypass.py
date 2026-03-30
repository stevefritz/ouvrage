"""Tests for SKIP_CREDENTIAL_CHECK / HAS_CLAUDE_BINARY bypass behaviour.

Scenarios covered:
- SKIP_CREDENTIAL_CHECK=true → dispatch_task works without Anthropic key
- SKIP_CREDENTIAL_CHECK=true → create_project works without PAT
- SKIP_CREDENTIAL_CHECK=true + PAT configured → still validates clone access
- SKIP_CREDENTIAL_CHECK=false (default) → existing validation fires (regression guard)
- HAS_CLAUDE_BINARY=true → dispatch_task works without Anthropic key
- Settings API exposes skip_credential_check flag to the frontend
"""

import json
from unittest.mock import AsyncMock, patch

import pytest


# ── ASGI helpers (same pattern as test_onboarding_guardrails) ───────────────

def _make_scope(method="GET", path="/dashboard/api/settings/user", user=None, no_user=False):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "session_user": {} if no_user else (user or {"id": 1, "email": "owner@localhost", "name": "Owner", "role": "owner"}),
    }


def _make_receive(body=None):
    raw = json.dumps(body).encode() if isinstance(body, dict) else (body or b"")

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


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def owner_user(db):
    user = await db.get_user_by_email("owner@localhost")
    assert user is not None
    await db.update_instance(owner_user_id=user["id"])
    return user


@pytest.fixture
async def user_without_anthropic_key(db, owner_user):
    """Owner user with NO Anthropic key."""
    return owner_user


@pytest.fixture
async def user_with_anthropic_key(db, owner_user):
    """Owner user WITH Anthropic key."""
    await db.update_user_credentials(owner_user["id"], anthropic_api_key="sk-ant-test-key")
    return owner_user


# ── dispatch_task: SKIP_CREDENTIAL_CHECK=true ────────────────────────────────


class TestDispatchSkipCredentialCheck:
    """SKIP_CREDENTIAL_CHECK=true lets dispatch_task through without an Anthropic key."""

    @pytest.fixture(autouse=True)
    def patch_context(self, owner_user):
        with patch("switchboard.server.handlers.tasks.get_request_user_id", return_value=owner_user["id"]):
            with patch("switchboard.server.handlers.tasks.get_request_is_token_auth", return_value=True):
                with patch("switchboard.server.handlers.tasks.get_request_is_worker", return_value=False):
                    yield

    async def test_skip_true_allows_dispatch_without_key(self, db, sample_project, user_without_anthropic_key, mock_git):
        """SKIP_CREDENTIAL_CHECK=true → no error even without Anthropic key."""
        from switchboard.server.handlers import tasks as tasks_module

        with patch.object(tasks_module, "SKIP_CREDENTIAL_CHECK", True):
            with patch.object(tasks_module, "HAS_CLAUDE_BINARY", False):
                from switchboard.server.handlers.tasks import _handle_dispatch_task

                result = await _handle_dispatch_task({
                    "project_id": "test-project",
                    "id": "test-project/bypass-task",
                    "goal": "Do something",
                    "held": True,
                })

        assert "error" not in result
        task = await db.get_task("test-project/bypass-task")
        assert task is not None

    async def test_skip_false_blocks_dispatch_without_key(self, db, sample_project, user_without_anthropic_key):
        """SKIP_CREDENTIAL_CHECK=false (default) → error when no Anthropic key."""
        from switchboard.server.handlers import tasks as tasks_module

        with patch.object(tasks_module, "SKIP_CREDENTIAL_CHECK", False):
            with patch.object(tasks_module, "HAS_CLAUDE_BINARY", False):
                from switchboard.server.handlers.tasks import _handle_dispatch_task

                result = await _handle_dispatch_task({
                    "project_id": "test-project",
                    "id": "test-project/blocked-task",
                    "goal": "Should be blocked",
                    "held": True,
                })

        assert "error" in result
        assert "Anthropic API key" in result["error"]
        task = await db.get_task("test-project/blocked-task")
        assert task is None


# ── dispatch_task: HAS_CLAUDE_BINARY=true ────────────────────────────────────


class TestDispatchClaudeBinaryDetection:
    """HAS_CLAUDE_BINARY=true also satisfies the Anthropic credential check."""

    @pytest.fixture(autouse=True)
    def patch_context(self, owner_user):
        with patch("switchboard.server.handlers.tasks.get_request_user_id", return_value=owner_user["id"]):
            with patch("switchboard.server.handlers.tasks.get_request_is_token_auth", return_value=True):
                with patch("switchboard.server.handlers.tasks.get_request_is_worker", return_value=False):
                    yield

    async def test_claude_binary_allows_dispatch_without_key(self, db, sample_project, user_without_anthropic_key, mock_git):
        """HAS_CLAUDE_BINARY=true → bypass even with SKIP_CREDENTIAL_CHECK=false."""
        from switchboard.server.handlers import tasks as tasks_module

        with patch.object(tasks_module, "SKIP_CREDENTIAL_CHECK", False):
            with patch.object(tasks_module, "HAS_CLAUDE_BINARY", True):
                from switchboard.server.handlers.tasks import _handle_dispatch_task

                result = await _handle_dispatch_task({
                    "project_id": "test-project",
                    "id": "test-project/binary-task",
                    "goal": "CC subscription auth",
                    "held": True,
                })

        assert "error" not in result
        task = await db.get_task("test-project/binary-task")
        assert task is not None

    async def test_no_binary_no_skip_still_blocks(self, db, sample_project, user_without_anthropic_key):
        """Neither bypass flag set → credential check still fires."""
        from switchboard.server.handlers import tasks as tasks_module

        with patch.object(tasks_module, "SKIP_CREDENTIAL_CHECK", False):
            with patch.object(tasks_module, "HAS_CLAUDE_BINARY", False):
                from switchboard.server.handlers.tasks import _handle_dispatch_task

                result = await _handle_dispatch_task({
                    "project_id": "test-project",
                    "id": "test-project/still-blocked",
                    "goal": "Still blocked",
                    "held": True,
                })

        assert "error" in result
        assert "Anthropic API key" in result["error"]


# ── create_project: SKIP_CREDENTIAL_CHECK=true ───────────────────────────────

_BASE_PROJECT_ARGS = {
    "id": "bypass-project",
    "repo": "https://github.com/acme/bypass-repo.git",
    "model": "sonnet",
    "review_model": "sonnet",
    "auto_test": True,
    "auto_review": True,
    "auto_pr": False,
    "auto_merge": False,
    "max_turns": 100,
    "max_wall_clock": 30,
}


class TestCreateProjectSkipCredentialCheck:
    """SKIP_CREDENTIAL_CHECK=true lets create_project proceed without a PAT."""

    async def test_skip_true_no_pat_creates_project(self, db):
        """SKIP_CREDENTIAL_CHECK=true + no PAT → project created without error."""
        from switchboard.server.handlers import projects as proj_module

        with patch.object(proj_module, "SKIP_CREDENTIAL_CHECK", True):
            with patch("switchboard.server.handlers.projects.db.get_instance_github_pat",
                       side_effect=ValueError("No PAT configured")):
                with patch("switchboard.server.handlers.projects.WORKTREE_BASE", "/work"):
                    result = await proj_module._handle_create_project(_BASE_PROJECT_ARGS)

        assert "error" not in result
        project = await db.get_project("bypass-project")
        assert project is not None

    async def test_skip_true_with_pat_still_validates_clone(self, db):
        """SKIP_CREDENTIAL_CHECK=true but PAT IS configured → clone validation still runs."""
        from switchboard.server.handlers import projects as proj_module

        with patch.object(proj_module, "SKIP_CREDENTIAL_CHECK", True):
            with patch("switchboard.server.handlers.projects.db.get_instance_github_pat",
                       return_value="ghp_sometoken"):
                # Make ls-remote fail to verify clone validation still fires
                with patch("switchboard.server.handlers.projects._validate_github_pat_for_repo",
                           return_value={"error": "GitHub PAT cannot access this repo. Check your token's permissions."}):
                    result = await proj_module._handle_create_project(_BASE_PROJECT_ARGS)

        assert "error" in result
        assert "access" in result["error"].lower() or "permissions" in result["error"].lower()

        project = await db.get_project("bypass-project")
        assert project is None

    async def test_skip_false_no_pat_blocked(self, db):
        """SKIP_CREDENTIAL_CHECK=false → PAT-absent error still fires (regression guard)."""
        from switchboard.server.handlers import projects as proj_module

        with patch.object(proj_module, "SKIP_CREDENTIAL_CHECK", False):
            with patch("switchboard.server.handlers.projects._validate_github_pat_for_repo",
                       return_value={"error": "Add your GitHub PAT in Settings before creating projects."}):
                result = await proj_module._handle_create_project(_BASE_PROJECT_ARGS)

        assert "error" in result
        assert "GitHub PAT" in result["error"]

        project = await db.get_project("bypass-project")
        assert project is None


# ── Settings API: skip_credential_check flag ─────────────────────────────────


class TestSettingsApiBypassFlag:
    """GET /dashboard/api/settings/user exposes skip_credential_check in anthropic object."""

    async def test_bypass_true_flag_returned(self, db, owner_user):
        """When bypass is active, anthropic.skip_credential_check=true in response."""
        from switchboard.dashboard import api as api_module

        with patch.object(api_module._settings, "SKIP_CREDENTIAL_CHECK", True):
            with patch.object(api_module._settings, "HAS_CLAUDE_BINARY", False):
                from switchboard.dashboard.api import handle_request

                scope = _make_scope(
                    method="GET",
                    path="/dashboard/api/settings/user",
                    user={"id": owner_user["id"], "email": "owner@localhost", "name": "Owner", "role": "owner"},
                )
                send = _Capture()
                await handle_request(scope, _make_receive(), send)

        assert send.status == 200
        body = send.json()
        assert "anthropic" in body
        assert body["anthropic"]["skip_credential_check"] is True

    async def test_bypass_false_flag_returned(self, db, owner_user):
        """When bypass is inactive, anthropic.skip_credential_check=false in response."""
        from switchboard.dashboard import api as api_module

        with patch.object(api_module._settings, "SKIP_CREDENTIAL_CHECK", False):
            with patch.object(api_module._settings, "HAS_CLAUDE_BINARY", False):
                from switchboard.dashboard.api import handle_request

                scope = _make_scope(
                    method="GET",
                    path="/dashboard/api/settings/user",
                    user={"id": owner_user["id"], "email": "owner@localhost", "name": "Owner", "role": "owner"},
                )
                send = _Capture()
                await handle_request(scope, _make_receive(), send)

        assert send.status == 200
        body = send.json()
        assert "anthropic" in body
        assert body["anthropic"]["skip_credential_check"] is False

    async def test_has_claude_binary_sets_flag_true(self, db, owner_user):
        """HAS_CLAUDE_BINARY=true also sets skip_credential_check=true."""
        from switchboard.dashboard import api as api_module

        with patch.object(api_module._settings, "SKIP_CREDENTIAL_CHECK", False):
            with patch.object(api_module._settings, "HAS_CLAUDE_BINARY", True):
                from switchboard.dashboard.api import handle_request

                scope = _make_scope(
                    method="GET",
                    path="/dashboard/api/settings/user",
                    user={"id": owner_user["id"], "email": "owner@localhost", "name": "Owner", "role": "owner"},
                )
                send = _Capture()
                await handle_request(scope, _make_receive(), send)

        assert send.status == 200
        body = send.json()
        assert body["anthropic"]["skip_credential_check"] is True
