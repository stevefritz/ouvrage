"""Tests for switchboard/db/users.py — user, instance, credentials, and API token CRUD."""

import pytest

from switchboard.crypto import is_fernet_token


# ===========================================================================
# User CRUD
# ===========================================================================


class TestUpdateUser:


    async def test_update_unknown_field_raises(self, db):
        user = await db.create_user(email="unk@example.com", name="Unknown")
        with pytest.raises(ValueError, match="Unknown user fields"):
            await db.update_user(user["id"], nonexistent_field="oops")


# ===========================================================================
# Instance (single row)
# ===========================================================================

class TestInstance:


    async def test_update_instance_unknown_field_raises(self, db):
        with pytest.raises(ValueError, match="Unknown instance fields"):
            await db.update_instance(hacker_field="pwned")


# ===========================================================================
# User credentials (upsert semantics)
# ===========================================================================

class TestUserCredentials:


    async def test_update_credentials_unknown_field_raises(self, db):
        user = await db.create_user(email="unkc@example.com", name="Unk Cred")
        with pytest.raises(ValueError, match="Unknown credential fields"):
            await db.update_user_credentials(user["id"], evil_field="drop table")


# ===========================================================================
# API tokens
# ===========================================================================

class TestApiTokens:


    async def test_validate_expired_token_returns_none(self, db):
        user = await db.create_user(email="exp@example.com", name="Exp User")
        result = await db.create_api_token(user["id"])
        # Manually set expires_at to the past
        import switchboard.db.connection as _conn
        async with _conn.get_db() as db_conn:
            await db_conn.execute(
                "UPDATE api_tokens SET expires_at = '2000-01-01T00:00:00' WHERE id = ?",
                (result["id"],),
            )
            await db_conn.commit()
        user_id = await db.validate_api_token(result["token"])
        assert user_id is None


# ===========================================================================
# Bootstrap migration
# ===========================================================================


# ===========================================================================
# Encryption of sensitive credential fields
# ===========================================================================

class TestEncryption:


    async def test_encryption_migration_encrypts_plaintext_values(self, db):
        """init_db() re-run encrypts any pre-existing plaintext credential values."""
        import switchboard.db.connection as _conn

        # Write a plaintext value directly to the DB (bypassing the ORM layer)
        inst = await db.get_instance()
        user_id = inst["owner_user_id"]
        async with _conn.get_db() as conn:
            await conn.execute(
                """INSERT OR IGNORE INTO user_credentials (user_id, notification_preferences, updated_at)
                   VALUES (?, '{}', '2026-01-01T00:00:00Z')""",
                (user_id,),
            )
            await conn.execute(
                "UPDATE user_credentials SET anthropic_api_key = ? WHERE user_id = ?",
                ("sk-plaintext-migrate", user_id),
            )
            await conn.commit()

        # Re-run init_db to trigger migration
        await db.init_db()

        # Now the value in the DB should be encrypted
        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT anthropic_api_key FROM user_credentials WHERE user_id = ?", (user_id,)
            )
        raw = rows[0]["anthropic_api_key"]
        assert is_fernet_token(raw), f"Expected encrypted value after migration, got: {raw!r}"

        # And reading via ORM returns plaintext
        creds = await db.get_user_credentials(user_id)
        assert creds["anthropic_api_key"] == "sk-plaintext-migrate"
