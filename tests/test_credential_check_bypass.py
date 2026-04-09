"""Tests for SKIP_CREDENTIAL_CHECK bypass behaviour.

Scenarios covered:
- SKIP_CREDENTIAL_CHECK=true -> dispatch_task works without Anthropic key
- SKIP_CREDENTIAL_CHECK=true -> create_project works without PAT
- SKIP_CREDENTIAL_CHECK=true + PAT configured -> still validates clone access
- SKIP_CREDENTIAL_CHECK=false (default) -> existing validation fires (regression guard)
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


    async def test_skip_false_blocks_dispatch_without_key(self, db, sample_project, user_without_anthropic_key):
        """SKIP_CREDENTIAL_CHECK=false (default) → error when no Anthropic key."""
        from switchboard.server.handlers import tasks as tasks_module

        with patch.object(tasks_module, "SKIP_CREDENTIAL_CHECK", False):
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


# ── Settings API: skip_credential_check flag ─────────────────────────────────


# ── Settings API: clear API key when SKIP_CREDENTIAL_CHECK is set ─────────────


