"""Tests for MCP API token auth: token creation, validation, user resolution,
and created_by/user_id stamping on write operations."""

import pytest
from unittest.mock import patch, AsyncMock


# ---------------------------------------------------------------------------
# Token format and validation
# ---------------------------------------------------------------------------

class TestTokenFormat:

    async def test_create_token_has_sb_prefix(self, db):
        user = await db.create_user(email="fmt@example.com", name="Format User")
        result = await db.create_api_token(user["id"], name="test")
        assert result["token"].startswith("sb_"), "Token must start with sb_"

    async def test_create_token_length(self, db):
        user = await db.create_user(email="len@example.com", name="Len User")
        result = await db.create_api_token(user["id"])
        assert len(result["token"]) == 67  # "sb_" + 64 hex chars

    async def test_validate_sb_prefixed_token(self, db):
        user = await db.create_user(email="v1@example.com", name="Validate1")
        result = await db.create_api_token(user["id"])
        assert result["token"].startswith("sb_")
        user_id = await db.validate_api_token(result["token"])
        assert user_id == user["id"]

    async def test_validate_token_without_prefix_fails(self, db):
        """Token stored as sb_xxx should not validate without the prefix."""
        user = await db.create_user(email="v2@example.com", name="Validate2")
        result = await db.create_api_token(user["id"])
        # Strip the sb_ prefix — should not validate
        raw_hex = result["token"][3:]
        user_id = await db.validate_api_token(raw_hex)
        assert user_id is None

    async def test_create_token_returns_name(self, db):
        user = await db.create_user(email="named@example.com", name="Named")
        result = await db.create_api_token(user["id"], name="my laptop")
        assert result["name"] == "my laptop"

    async def test_list_tokens_excludes_hash(self, db):
        user = await db.create_user(email="lst@example.com", name="List")
        await db.create_api_token(user["id"], name="token1")
        tokens = await db.list_api_tokens(user["id"])
        assert len(tokens) == 1
        assert "token_hash" not in tokens[0]
        assert "token" not in tokens[0]
        assert tokens[0]["name"] == "token1"

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
        from ouvrage.server.app import _resolve_mcp_user_id
        user = await db.create_user(email="auth1@example.com", name="Auth1")
        result = await db.create_api_token(user["id"])
        scope = _make_scope(f"Bearer {result['token']}")
        user_id, is_token_auth = await _resolve_mcp_user_id(scope)
        assert user_id == user["id"]
        assert is_token_auth is True

    async def test_no_token_falls_back_to_instance_owner(self, db):
        from ouvrage.server.app import _resolve_mcp_user_id
        # init_db creates an instance owner via bootstrap migration
        scope = _make_scope(None)
        user_id, is_token_auth = await _resolve_mcp_user_id(scope)
        assert user_id is not None
        assert is_token_auth is False

    async def test_invalid_token_returns_none(self, db):
        from ouvrage.server.app import _resolve_mcp_user_id
        scope = _make_scope("Bearer sb_deadbeef" + "0" * 60)
        user_id, is_token_auth = await _resolve_mcp_user_id(scope)
        assert user_id is None
        assert is_token_auth is False

    async def test_no_token_is_token_auth_false(self, db):
        from ouvrage.server.app import _resolve_mcp_user_id
        scope = _make_scope(None)
        _, is_token_auth = await _resolve_mcp_user_id(scope)
        assert is_token_auth is False

    async def test_malformed_auth_header_treated_as_invalid(self, db):
        from ouvrage.server.app import _resolve_mcp_user_id
        scope = _make_scope("Basic somebase64value")
        user_id, is_token_auth = await _resolve_mcp_user_id(scope)
        # No Bearer token extracted → falls back to instance owner
        assert is_token_auth is False


# ---------------------------------------------------------------------------
# created_by stamping on DB write operations
# ---------------------------------------------------------------------------

class TestCreatedByStamping:

    async def test_create_project_stamps_created_by(self, db):
        user = await db.create_user(email="proj@example.com", name="Proj")
        project = await db.create_project(
            id="stamped-project",
            repo="git@github.com:x/y.git",
            working_dir="/work/y",
            created_by=user["id"],
        )
        assert project["created_by"] == user["id"]

    async def test_create_project_no_created_by_is_null(self, db):
        project = await db.create_project(
            id="no-stamp-project",
            repo="git@github.com:x/z.git",
            working_dir="/work/z",
        )
        assert project["created_by"] is None

    async def test_create_component_stamps_created_by(self, db, sample_project):
        user = await db.create_user(email="comp@example.com", name="Comp")
        comp = await db.create_component(
            id="stamped-comp",
            project_id=sample_project["id"],
            name="Stamped Component",
            created_by=user["id"],
        )
        assert comp["created_by"] == user["id"]

    async def test_create_conversation_stamps_created_by(self, db, sample_project):
        user = await db.create_user(email="conv@example.com", name="Conv")
        conv = await db.create_conversation(
            id="stamped-conv",
            project=sample_project["id"],
            goal="Test goal",
            created_by=user["id"],
        )
        assert conv["created_by"] == user["id"]

    async def test_create_task_stamps_created_by_and_dispatched_by(self, db, sample_project):
        user = await db.create_user(email="task@example.com", name="Task")
        task = await db.create_task(
            id="test-project/stamped-task",
            project_id=sample_project["id"],
            goal="Stamped task",
            created_by=user["id"],
            dispatched_by=user["id"],
        )
        assert task["created_by"] == user["id"]
        assert task["dispatched_by"] == user["id"]

    async def test_create_task_no_stamps_is_null(self, db, sample_project):
        task = await db.create_task(
            id="test-project/no-stamp-task",
            project_id=sample_project["id"],
            goal="No stamp",
        )
        assert task["created_by"] is None
        assert task["dispatched_by"] is None


# ---------------------------------------------------------------------------
# user_id stamping on messages
# ---------------------------------------------------------------------------

class TestMessageUserIdStamping:

    async def test_post_message_stamps_user_id(self, db, sample_project):
        user = await db.create_user(email="msg@example.com", name="Msg")
        conv = await db.create_conversation(
            id="msg-conv", project=sample_project["id"], goal="test"
        )
        msg = await db.post_message(
            conversation_id="msg-conv",
            author="stephen",
            content="Hello",
            user_id=user["id"],
        )
        assert msg["user_id"] == user["id"]

    async def test_post_message_no_user_id_is_null(self, db, sample_project):
        conv = await db.create_conversation(
            id="msg-conv-null", project=sample_project["id"], goal="test"
        )
        msg = await db.post_message(
            conversation_id="msg-conv-null",
            author="cc-worker",
            content="Hello",
        )
        assert msg["user_id"] is None

    async def test_post_task_message_stamps_user_id(self, db, sample_task):
        user = await db.create_user(email="tmsg@example.com", name="TaskMsg")
        msg = await db.post_task_message(
            task_id=sample_task["id"],
            author="stephen",
            content="Review this",
            user_id=user["id"],
        )
        assert msg["user_id"] == user["id"]

    async def test_post_task_message_no_user_id_is_null(self, db, sample_task):
        msg = await db.post_task_message(
            task_id=sample_task["id"],
            author="cc-worker",
            content="Progress update",
        )
        assert msg["user_id"] is None


# ---------------------------------------------------------------------------
# Context var: _resolve_message_user_id logic
# ---------------------------------------------------------------------------

class TestResolveMessageUserId:
    """Test the handler-level helper that decides when to set user_id."""

    def test_token_auth_always_stamps(self):
        from ouvrage.server.context import set_request_context
        from ouvrage.server.handlers.conversations import _resolve_message_user_id
        set_request_context(user_id=42, is_token_auth=True)
        # Even system actors get stamped when token auth is present
        assert _resolve_message_user_id("cc-worker") == 42
        assert _resolve_message_user_id("dispatcher") == 42
        assert _resolve_message_user_id("stephen") == 42

    def test_fallback_skips_system_actors(self):
        from ouvrage.server.context import set_request_context
        from ouvrage.server.handlers.conversations import _resolve_message_user_id
        set_request_context(user_id=99, is_token_auth=False)
        # System actors are not stamped in fallback mode
        assert _resolve_message_user_id("cc-worker") is None
        assert _resolve_message_user_id("dispatcher") is None
        assert _resolve_message_user_id("switchboard") is None

    def test_fallback_stamps_human_authors(self):
        from ouvrage.server.context import set_request_context
        from ouvrage.server.handlers.conversations import _resolve_message_user_id
        set_request_context(user_id=99, is_token_auth=False)
        # Non-system authors get stamped in fallback mode
        assert _resolve_message_user_id("stephen") == 99
        assert _resolve_message_user_id("claude-ai") == 99

    def test_no_user_id_returns_none(self):
        from ouvrage.server.context import set_request_context
        from ouvrage.server.handlers.conversations import _resolve_message_user_id
        set_request_context(user_id=None, is_token_auth=False)
        assert _resolve_message_user_id("stephen") is None
        assert _resolve_message_user_id("cc-worker") is None
