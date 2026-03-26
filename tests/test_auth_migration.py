"""Tests for auth migration: run_migrate_auth creates user, instance, seeds client, is idempotent."""

import pytest
from unittest.mock import patch, AsyncMock


class TestMigrateAuth:

    async def test_creates_owner_user(self, db):
        """migrate-auth creates user with provided email and name."""
        from switchboard.migrate import run_migrate_auth
        from switchboard.db.users import get_user_by_email

        with patch("switchboard.auth.oauth.seed_default_client", new=AsyncMock()):
            result = await run_migrate_auth(
                email="admin@example.com",
                name="Admin User",
                password_hash="$argon2id$v=19$m=65536,t=3,p=4$fakehash",
                slug="myinstance",
            )

        user = await get_user_by_email("admin@example.com")
        assert user is not None
        assert user["name"] == "Admin User"
        assert user["role"] == "owner"
        assert result["owner_id"] == user["id"]

    async def test_creates_instance_with_slug(self, db):
        """migrate-auth updates instance slug and name."""
        from switchboard.migrate import run_migrate_auth
        from switchboard.db.users import get_instance

        with patch("switchboard.auth.oauth.seed_default_client", new=AsyncMock()):
            await run_migrate_auth(
                email="admin@example.com",
                name="Admin",
                password_hash="$argon2id$fakehash",
                slug="my-slug",
                instance_name="My Instance",
            )

        inst = await get_instance()
        assert inst["slug"] == "my-slug"
        assert inst["name"] == "My Instance"

    async def test_seeds_oauth_client(self, db):
        """migrate-auth seeds claude-mcp OAuth client."""
        from switchboard.migrate import run_migrate_auth
        from switchboard.db.connection import get_db

        await run_migrate_auth(
            email="admin@example.com",
            name="Admin",
            password_hash="$argon2id$fakehash",
            slug="default",
        )

        async with get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT client_id FROM oauth_clients WHERE client_id = 'claude-mcp'"
            )
        assert rows, "claude-mcp client was not seeded"

    async def test_idempotent_same_email(self, db):
        """Running migrate-auth twice with same email skips on second call."""
        from switchboard.migrate import run_migrate_auth
        from switchboard.db.users import list_users

        with patch("switchboard.auth.oauth.seed_default_client", new=AsyncMock()):
            first = await run_migrate_auth(
                email="admin@example.com",
                name="Admin",
                password_hash="$argon2id$fakehash",
                slug="default",
            )
            second = await run_migrate_auth(
                email="admin@example.com",
                name="Admin",
                password_hash="$argon2id$fakehash",
                slug="default",
            )

        assert first["status"] == "migrated"
        assert second["status"] == "already_migrated"

        # Exactly one user with that email
        users = await list_users()
        matching = [u for u in users if u["email"] == "admin@example.com"]
        assert len(matching) == 1

    async def test_replaces_owner_placeholder(self, db):
        """migrate-auth replaces owner@localhost placeholder, preserving user_id for FK integrity."""
        from switchboard.migrate import run_migrate_auth
        from switchboard.db.users import get_user_by_email

        # Bootstrap creates owner@localhost — confirm it's there
        placeholder = await get_user_by_email("owner@localhost")
        assert placeholder is not None, "Bootstrap should have created owner@localhost"
        placeholder_id = placeholder["id"]

        with patch("switchboard.auth.oauth.seed_default_client", new=AsyncMock()):
            result = await run_migrate_auth(
                email="real@example.com",
                name="Real Owner",
                password_hash="$argon2id$v=19$fakehash",
                slug="production",
            )

        # Placeholder email is gone
        old = await get_user_by_email("owner@localhost")
        assert old is None

        # Real user exists with same id (FK backfills preserved)
        real = await get_user_by_email("real@example.com")
        assert real is not None
        assert real["id"] == placeholder_id, "Real user should reuse placeholder's user_id"
        assert result["owner_id"] == placeholder_id

    async def test_backfills_project_fks(self, db, sample_project):
        """migrate-auth backfills NULL created_by on projects."""
        from switchboard.migrate import run_migrate_auth
        from switchboard.db.connection import get_db

        # Manually null out created_by
        async with get_db() as conn:
            await conn.execute("UPDATE projects SET created_by = NULL")
            await conn.commit()

        with patch("switchboard.auth.oauth.seed_default_client", new=AsyncMock()):
            await run_migrate_auth(
                email="admin@example.com",
                name="Admin",
                password_hash="$argon2id$fakehash",
                slug="default",
            )

        async with get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT id FROM projects WHERE created_by IS NULL"
            )
        assert len(rows) == 0, "All projects should have created_by set after migration"

    async def test_returns_client_id_and_secret(self, db):
        """run_migrate_auth result includes client_id and client_secret."""
        from switchboard.migrate import run_migrate_auth

        result = await run_migrate_auth(
            email="admin@example.com",
            name="Admin",
            password_hash="$argon2id$fakehash",
            slug="default",
        )

        assert result["status"] == "migrated"
        assert result["client_id"] == "claude-mcp"
        assert result["client_secret"]  # non-empty string

    async def test_instance_owner_points_to_real_user(self, db):
        """Instance owner_user_id points to the real owner after migration."""
        from switchboard.migrate import run_migrate_auth
        from switchboard.db.users import get_instance, get_user_by_email

        with patch("switchboard.auth.oauth.seed_default_client", new=AsyncMock()):
            result = await run_migrate_auth(
                email="owner@myco.com",
                name="Owner",
                password_hash="$argon2id$fakehash",
                slug="myco",
            )

        inst = await get_instance()
        assert inst["owner_user_id"] == result["owner_id"]
