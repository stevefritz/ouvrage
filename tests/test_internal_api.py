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

    async def test_config_returns_404_in_local_mode(self, db):
        status, _ = await _call("POST", "/internal/config", _json({}))
        assert status == 404

    async def test_bootstrap_user_returns_404_in_local_mode(self, db):
        status, _ = await _call("POST", "/internal/bootstrap-user",
                                _json({"email": "a@b.com", "role": "owner"}))
        assert status == 404

    async def test_usage_returns_404_in_local_mode(self, db):
        status, _ = await _call("GET", "/internal/usage")
        assert status == 404

    async def test_unknown_path_returns_404_in_local_mode(self, db):
        status, _ = await _call("GET", "/internal/whatever")
        assert status == 404


# ── Auth checks ──────────────────────────────────────────────────────────────

class TestAuth:

    @pytest.fixture(autouse=True)
    def saas_mode(self):
        with patch("switchboard.internal.api.AUTH_MODE", "saas"), \
             patch("switchboard.internal.api.INTERNAL_API_TOKEN", "secret-token"):
            yield

    async def test_valid_token_accepted(self, db):
        status, data = await _call("GET", "/internal/usage", token="secret-token")
        assert status == 200

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

    async def test_sets_concurrency_limit(self, db):
        status, data = await _call("POST", "/internal/config",
                                   _json({"concurrency_limit": 3}))
        assert status == 200
        assert data["ok"] is True
        assert data["concurrency_limit"] == 3

    async def test_sets_max_projects(self, db):
        status, data = await _call("POST", "/internal/config",
                                   _json({"max_projects": 10}))
        assert status == 200
        assert data["ok"] is True
        assert data["max_projects"] == 10

    async def test_sets_both_fields(self, db):
        status, data = await _call("POST", "/internal/config",
                                   _json({"concurrency_limit": 5, "max_projects": 20}))
        assert status == 200
        assert data["concurrency_limit"] == 5
        assert data["max_projects"] == 20

    async def test_config_persists(self, db):
        await _call("POST", "/internal/config", _json({"concurrency_limit": 7}))
        cfg = await db.get_instance_config()
        assert cfg["concurrency_limit"] == 7

    async def test_second_set_overwrites(self, db):
        await _call("POST", "/internal/config", _json({"concurrency_limit": 3}))
        status, data = await _call("POST", "/internal/config",
                                   _json({"concurrency_limit": 8}))
        assert status == 200
        assert data["concurrency_limit"] == 8
        cfg = await db.get_instance_config()
        assert cfg["concurrency_limit"] == 8

    async def test_empty_body_accepted(self, db):
        """Empty body → set both to None (clear overrides)."""
        status, data = await _call("POST", "/internal/config", _json({}))
        assert status == 200
        assert data["ok"] is True

    async def test_rejects_unknown_field(self, db):
        status, data = await _call("POST", "/internal/config",
                                   _json({"concurrency_limit": 3, "bad_field": "x"}))
        assert status == 422
        assert data["error"] == "unknown_fields"

    async def test_rejects_multiple_unknown_fields(self, db):
        status, data = await _call("POST", "/internal/config",
                                   _json({"foo": 1, "bar": 2}))
        assert status == 422
        assert data["error"] == "unknown_fields"

    async def test_rejects_non_integer_concurrency_limit(self, db):
        status, data = await _call("POST", "/internal/config",
                                   _json({"concurrency_limit": "three"}))
        assert status == 422
        assert data["error"] == "invalid_type"

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

    async def test_creates_new_user(self, db):
        status, data = await _call("POST", "/internal/bootstrap-user",
                                   _json({"email": "newuser@example.com", "role": "owner"}))
        assert status == 200
        assert data["ok"] is True
        assert data["created"] is True

    async def test_user_actually_stored(self, db):
        await _call("POST", "/internal/bootstrap-user",
                    _json({"email": "stored@example.com", "role": "member"}))
        user = await db.get_user_by_email("stored@example.com")
        assert user is not None
        assert user["role"] == "member"

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

    async def test_idempotent_does_not_modify_existing_user(self, db):
        await db.create_user(email="alice@example.com", name="Alice", role="owner")
        await _call("POST", "/internal/bootstrap-user",
                    _json({"email": "alice@example.com", "role": "member"}))
        user = await db.get_user_by_email("alice@example.com")
        assert user["role"] == "owner"  # not changed to member

    async def test_missing_email_returns_422(self, db):
        status, data = await _call("POST", "/internal/bootstrap-user",
                                   _json({"role": "owner"}))
        assert status == 422
        assert data["error"] == "missing_field"

    async def test_email_normalised_to_lowercase(self, db):
        status, data = await _call("POST", "/internal/bootstrap-user",
                                   _json({"email": "UPPER@Example.COM", "role": "member"}))
        assert status == 200
        user = await db.get_user_by_email("upper@example.com")
        assert user is not None

    async def test_default_role_is_member(self, db):
        await _call("POST", "/internal/bootstrap-user",
                    _json({"email": "norole@example.com"}))
        user = await db.get_user_by_email("norole@example.com")
        assert user["role"] == "member"


# ── GET /internal/usage ──────────────────────────────────────────────────────

class TestUsage:

    @pytest.fixture(autouse=True)
    def saas_mode(self):
        with patch("switchboard.internal.api.AUTH_MODE", "saas"), \
             patch("switchboard.internal.api.INTERNAL_API_TOKEN", "secret-token"):
            yield

    async def test_usage_empty_db(self, db):
        status, data = await _call("GET", "/internal/usage")
        assert status == 200
        assert data["tasks_this_month"] == 0
        assert data["total_cost_usd"] == 0.0
        assert data["active_tasks"] == 0
        assert data["current_concurrency"] == 0
        assert data["project_count"] == 0

    async def test_usage_counts_tasks_this_month(self, db, sample_project):
        await db.create_task(id="p/t1", project_id="test-project", goal="Task 1")
        await db.create_task(id="p/t2", project_id="test-project", goal="Task 2")
        status, data = await _call("GET", "/internal/usage")
        assert status == 200
        assert data["tasks_this_month"] == 2

    async def test_usage_counts_active_tasks(self, db, sample_project):
        t = await db.create_task(id="p/active", project_id="test-project", goal="A")
        await db.update_task(t["id"], status="working")
        status, data = await _call("GET", "/internal/usage")
        assert status == 200
        assert data["active_tasks"] == 1
        assert data["current_concurrency"] == 1

    async def test_usage_current_concurrency_equals_active_tasks(self, db, sample_project):
        t1 = await db.create_task(id="p/w1", project_id="test-project", goal="W1")
        t2 = await db.create_task(id="p/w2", project_id="test-project", goal="W2")
        await db.update_task(t1["id"], status="working")
        await db.update_task(t2["id"], status="working")
        status, data = await _call("GET", "/internal/usage")
        assert status == 200
        assert data["active_tasks"] == data["current_concurrency"] == 2

    async def test_usage_sums_cost(self, db, sample_project):
        t1 = await db.create_task(id="p/c1", project_id="test-project", goal="C1")
        t2 = await db.create_task(id="p/c2", project_id="test-project", goal="C2")
        await db.update_task(t1["id"], total_cost_usd=5.50)
        await db.update_task(t2["id"], total_cost_usd=10.25)
        status, data = await _call("GET", "/internal/usage")
        assert status == 200
        assert abs(data["total_cost_usd"] - 15.75) < 0.001

    async def test_usage_has_required_keys(self, db):
        status, data = await _call("GET", "/internal/usage")
        assert status == 200
        assert "tasks_this_month" in data
        assert "total_cost_usd" in data
        assert "active_tasks" in data
        assert "current_concurrency" in data
        assert "project_count" in data

    async def test_usage_projects_count(self, db):
        await db.create_project(id="proj-1", repo="https://example.com/1.git", working_dir="/work/proj1")
        await db.create_project(id="proj-2", repo="https://example.com/2.git", working_dir="/work/proj2")
        status, data = await _call("GET", "/internal/usage")
        assert status == 200
        assert data["project_count"] == 2


# ── Instance config DB helper ────────────────────────────────────────────────

class TestInstanceConfig:

    async def test_get_instance_config_defaults_to_none(self, db):
        cfg = await db.get_instance_config()
        assert cfg["concurrency_limit"] is None
        assert cfg["max_projects"] is None

    async def test_get_concurrency_limit_falls_back_to_default(self, db):
        from switchboard.config.constants import DEFAULT_MAX_CONCURRENT
        limit = await db.get_concurrency_limit()
        assert limit == DEFAULT_MAX_CONCURRENT

    async def test_get_concurrency_limit_uses_db_value(self, db):
        await db.set_instance_config(concurrency_limit=2)
        limit = await db.get_concurrency_limit()
        assert limit == 2

    async def test_set_instance_config_upsert(self, db):
        await db.set_instance_config(concurrency_limit=3, max_projects=5)
        cfg = await db.get_instance_config()
        assert cfg["concurrency_limit"] == 3
        assert cfg["max_projects"] == 5

    async def test_set_instance_config_overwrites(self, db):
        await db.set_instance_config(concurrency_limit=3)
        await db.set_instance_config(concurrency_limit=9)
        cfg = await db.get_instance_config()
        assert cfg["concurrency_limit"] == 9
