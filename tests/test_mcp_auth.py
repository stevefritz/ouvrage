"""Tests for MCP API token auth: token creation, validation, user resolution,
and created_by/user_id stamping on write operations."""

import pytest
from unittest.mock import patch, AsyncMock


# ---------------------------------------------------------------------------
# Token format and validation
# ---------------------------------------------------------------------------

class TestTokenFormat:


    async def test_revoke_token(self, db):
        user = await db.create_user(email="rvk@example.com", name="Revoke")
        result = await db.create_api_token(user["id"])
        deleted = await db.revoke_api_token(result["id"])
        assert deleted is True
        assert await db.validate_api_token(result["token"]) is None


# ---------------------------------------------------------------------------
# _resolve_mcp_user_id logic (tested via app module)
# ---------------------------------------------------------------------------

def _make_scope(auth_header: str | None = None) -> dict:
    """Build a minimal ASGI scope with optional Authorization header."""
    headers = []
    if auth_header:
        headers.append((b"authorization", auth_header.encode()))
    return {"type": "http", "headers": headers}


class TestResolveMcpUserId:

    async def test_valid_token_returns_user_id_and_is_token_auth(self, db):
        from switchboard.server.app import _resolve_mcp_user_id
        user = await db.create_user(email="auth1@example.com", name="Auth1")
        result = await db.create_api_token(user["id"])
        scope = _make_scope(f"Bearer {result['token']}")
        user_id, is_token_auth = await _resolve_mcp_user_id(scope)
        assert user_id == user["id"]
        assert is_token_auth is True


    async def test_invalid_token_returns_none(self, db):
        from switchboard.server.app import _resolve_mcp_user_id
        scope = _make_scope("Bearer sb_deadbeef" + "0" * 60)
        user_id, is_token_auth = await _resolve_mcp_user_id(scope)
        assert user_id is None
        assert is_token_auth is False


    async def test_malformed_auth_header_treated_as_invalid(self, db):
        from switchboard.server.app import _resolve_mcp_user_id
        scope = _make_scope("Basic somebase64value")
        user_id, is_token_auth = await _resolve_mcp_user_id(scope)
        # No Bearer token extracted → falls back to instance owner
        assert is_token_auth is False


# ---------------------------------------------------------------------------
# created_by stamping on DB write operations
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# user_id stamping on messages
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Context var: _resolve_message_user_id logic
# ---------------------------------------------------------------------------

class TestResolveMessageUserId:
    """Test the handler-level helper that decides when to set user_id."""

    def test_token_auth_always_stamps(self):
        from switchboard.server.context import set_request_context
        from switchboard.server.handlers.conversations import _resolve_message_user_id
        set_request_context(user_id=42, is_token_auth=True)
        # Even system actors get stamped when token auth is present
        assert _resolve_message_user_id("cc-worker") == 42
        assert _resolve_message_user_id("dispatcher") == 42
        assert _resolve_message_user_id("stephen") == 42

    def test_fallback_skips_system_actors(self):
        from switchboard.server.context import set_request_context
        from switchboard.server.handlers.conversations import _resolve_message_user_id
        set_request_context(user_id=99, is_token_auth=False)
        # System actors are not stamped in fallback mode
        assert _resolve_message_user_id("cc-worker") is None
        assert _resolve_message_user_id("dispatcher") is None
        assert _resolve_message_user_id("switchboard") is None


