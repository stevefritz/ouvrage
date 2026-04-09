"""Tests for the /internal/* endpoints.

Covers:
- Valid Bearer token → 200
- Invalid token → 401
- Missing Authorization header → 401
- AUTH_MODE=local → 404 for all routes
- POST /internal/config sets values, persists, rejects unknown fields
- POST /internal/bootstrap-user creates new user, skips existing, requires email
- GET /internal/usage returns correct counts
- Concurrency limit read from instance_config
"""

import json
from unittest.mock import patch

import pytest

from switchboard.internal.api import handle_request


# ── ASGI call helper ─────────────────────────────────────────────────────────

async def _call(
    method: str = "GET",
    path: str = "/internal/usage",
    body: bytes = b"",
    token: str | None = "secret-token",
):
    """Call handle_request and return (status, body_dict_or_bytes)."""
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": headers,
    }

    _body = body
    _more = False

    async def receive():
        nonlocal _more
        if not _more:
            _more = True
            return {"type": "http.request", "body": _body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    status = None
    resp_body = b""

    async def send(message):
        nonlocal status, resp_body
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            resp_body += message.get("body", b"")

    await handle_request(scope, receive, send)
    try:
        data = json.loads(resp_body)
    except (json.JSONDecodeError, ValueError):
        data = resp_body
    return status, data


def _json(data: dict) -> bytes:
    return json.dumps(data).encode()


# ── AUTH_MODE=local ──────────────────────────────────────────────────────────

class TestLocalMode:

    @pytest.fixture(autouse=True)
    def local_mode(self):
        with patch("switchboard.internal.api.AUTH_MODE", "local"):
            yield


    async def test_bootstrap_user_returns_404_in_local_mode(self, db):
        status, _ = await _call("POST", "/internal/bootstrap-user",
                                _json({"email": "a@b.com", "role": "owner"}))
        assert status == 404


# ── Auth checks ──────────────────────────────────────────────────────────────

class TestAuth:

    @pytest.fixture(autouse=True)
    def saas_mode(self):
        with patch("switchboard.internal.api.AUTH_MODE", "saas"), \
             patch("switchboard.internal.api.INTERNAL_API_TOKEN", "secret-token"):
            yield


    async def test_invalid_token_rejected(self, db):
        status, data = await _call("GET", "/internal/usage", token="wrong-token")
        assert status == 401
        assert data["error"] == "unauthorized"

    async def test_missing_auth_header_rejected(self, db):
        status, data = await _call("GET", "/internal/usage", token=None)
        assert status == 401
        assert data["error"] == "unauthorized"

    async def test_empty_internal_api_token_rejects_all(self, db):
        """If INTERNAL_API_TOKEN is not configured, all requests fail."""
        with patch("switchboard.internal.api.INTERNAL_API_TOKEN", None):
            status, _ = await _call("GET", "/internal/usage", token="anything")
            assert status == 401


# ── POST /internal/config ────────────────────────────────────────────────────

class TestConfig:

    @pytest.fixture(autouse=True)
    def saas_mode(self):
        with patch("switchboard.internal.api.AUTH_MODE", "saas"), \
             patch("switchboard.internal.api.INTERNAL_API_TOKEN", "secret-token"):
            yield


    async def test_config_persists(self, db):
        await _call("POST", "/internal/config", _json({"concurrency_limit": 7}))
        cfg = await db.get_instance_config()
        assert cfg["concurrency_limit"] == 7


    async def test_rejects_multiple_unknown_fields(self, db):
        status, data = await _call("POST", "/internal/config",
                                   _json({"foo": 1, "bar": 2}))
        assert status == 422
        assert data["error"] == "unknown_fields"


    async def test_rejects_boolean_concurrency_limit(self, db):
        # Python: True is a bool subclass of int — must be rejected
        status, data = await _call("POST", "/internal/config",
                                   _json({"concurrency_limit": True}))
        assert status == 422


# ── POST /internal/bootstrap-user ───────────────────────────────────────────

class TestBootstrapUser:

    @pytest.fixture(autouse=True)
    def saas_mode(self):
        with patch("switchboard.internal.api.AUTH_MODE", "saas"), \
             patch("switchboard.internal.api.INTERNAL_API_TOKEN", "secret-token"):
            yield


    async def test_idempotent_existing_email(self, db):
        # Create first time
        await _call("POST", "/internal/bootstrap-user",
                    _json({"email": "existing@example.com", "role": "owner"}))
        # Call again — should succeed with created=False
        status, data = await _call("POST", "/internal/bootstrap-user",
                                   _json({"email": "existing@example.com", "role": "owner"}))
        assert status == 200
        assert data["ok"] is True
        assert data["created"] is False


    async def test_missing_email_returns_422(self, db):
        status, data = await _call("POST", "/internal/bootstrap-user",
                                   _json({"role": "owner"}))
        assert status == 422
        assert data["error"] == "missing_field"


# ── GET /internal/usage ──────────────────────────────────────────────────────

class TestUsage:

    @pytest.fixture(autouse=True)
    def saas_mode(self):
        with patch("switchboard.internal.api.AUTH_MODE", "saas"), \
             patch("switchboard.internal.api.INTERNAL_API_TOKEN", "secret-token"):
            yield


    async def test_usage_counts_active_tasks(self, db, sample_project):
        t = await db.create_task(id="p/active", project_id="test-project", goal="A")
        await db.update_task(t["id"], status="working")
        status, data = await _call("GET", "/internal/usage")
        assert status == 200
        assert data["active_tasks"] == 1
        assert data["current_concurrency"] == 1


# ── Instance config DB helper ────────────────────────────────────────────────

class TestInstanceConfig:


    async def test_get_concurrency_limit_uses_db_value(self, db):
        await db.set_instance_config(concurrency_limit=2)
        limit = await db.get_concurrency_limit()
        assert limit == 2


# ── POST /internal/config — trial_ends_at ────────────────────────────────────

class TestConfigTrialEndsAt:

    @pytest.fixture(autouse=True)
    def saas_mode(self):
        with patch("switchboard.internal.api.AUTH_MODE", "saas"), \
             patch("switchboard.internal.api.INTERNAL_API_TOKEN", "secret-token"):
            yield


    async def test_rejects_non_string_trial_ends_at(self, db):
        status, data = await _call("POST", "/internal/config",
                                   _json({"trial_ends_at": 12345}))
        assert status == 422
        assert data["error"] == "invalid_type"
