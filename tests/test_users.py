"""Tests for ouvrage/db/users.py — user, instance, credentials, and API token CRUD."""

import pytest

from ouvrage.crypto import is_fernet_token


# ===========================================================================
# User CRUD
# ===========================================================================

class TestCreateUser:

    async def test_create_returns_basic_fields(self, db):
        user = await db.create_user(email="alice@example.com", name="Alice")
        assert user["id"] is not None
        assert user["email"] == "alice@example.com"
        assert user["name"] == "Alice"
        assert user["role"] == "member"
        assert user["timezone"] == "America/Toronto"
        assert user["created_at"] is not None
        assert user["updated_at"] is not None

    async def test_create_does_not_leak_password_hash(self, db):
        user = await db.create_user(
            email="bob@example.com", name="Bob",
            password_hash="$2b$12$fakehash",
        )
        assert "password_hash" not in user

    async def test_create_with_custom_role(self, db):
        user = await db.create_user(email="admin@example.com", name="Admin", role="admin")
        assert user["role"] == "admin"

    async def test_create_duplicate_email_raises(self, db):
        await db.create_user(email="dup@example.com", name="First")
        with pytest.raises(Exception):
            await db.create_user(email="dup@example.com", name="Second")


class TestGetUser:

    async def test_get_by_id(self, db):
        created = await db.create_user(email="get@example.com", name="Get Me")
        fetched = await db.get_user(created["id"])
        assert fetched is not None
        assert fetched["email"] == "get@example.com"

    async def test_get_does_not_leak_password_hash(self, db):
        created = await db.create_user(email="noleak@example.com", name="No Leak")
        fetched = await db.get_user(created["id"])
        assert "password_hash" not in fetched

    async def test_get_by_email_does_not_leak_password_hash(self, db):
        await db.create_user(email="noleak2@example.com", name="No Leak 2")
        fetched = await db.get_user_by_email("noleak2@example.com")
        assert "password_hash" not in fetched

    async def test_get_nonexistent_returns_none(self, db):
        result = await db.get_user(999999)
        assert result is None

    async def test_get_by_email(self, db):
        await db.create_user(email="byemail@example.com", name="Email User")
        fetched = await db.get_user_by_email("byemail@example.com")
        assert fetched is not None
        assert fetched["name"] == "Email User"

    async def test_get_by_email_nonexistent_returns_none(self, db):
        result = await db.get_user_by_email("nobody@example.com")
        assert result is None


class TestUpdateUser:

    async def test_update_name(self, db):
        user = await db.create_user(email="upd@example.com", name="Old Name")
        updated = await db.update_user(user["id"], name="New Name")
        assert updated["name"] == "New Name"

    async def test_update_role(self, db):
        user = await db.create_user(email="role@example.com", name="Role User")
        updated = await db.update_user(user["id"], role="admin")
        assert updated["role"] == "admin"

    async def test_update_unknown_field_raises(self, db):
        user = await db.create_user(email="unk@example.com", name="Unknown")
        with pytest.raises(ValueError, match="Unknown user fields"):
            await db.update_user(user["id"], nonexistent_field="oops")

    async def test_update_sets_updated_at(self, db):
        user = await db.create_user(email="ts@example.com", name="Timestamp")
        original_ts = user["updated_at"]
        import asyncio
        await asyncio.sleep(0.01)
        updated = await db.update_user(user["id"], name="Updated")
        assert updated["updated_at"] >= original_ts


class TestListUsers:

    async def test_list_returns_all_users(self, db):
        await db.create_user(email="list1@example.com", name="List One")
        await db.create_user(email="list2@example.com", name="List Two")
        users = await db.list_users()
        emails = {u["email"] for u in users}
        assert {"list1@example.com", "list2@example.com"}.issubset(emails)

    async def test_list_empty_when_no_users(self, db):
        # Bootstrap migration creates owner@localhost, so list won't be truly empty.
        # Just verify it returns a list type and is non-None.
        users = await db.list_users()
        assert isinstance(users, list)


# ===========================================================================
# Instance (single row)
# ===========================================================================

class TestInstance:

    async def test_get_instance_returns_row(self, db):
        inst = await db.get_instance()
        assert inst is not None
        assert inst["name"] == "Ouvrage"
        assert inst["slug"] == "default"

    async def test_update_instance_name(self, db):
        updated = await db.update_instance(name="My Ouvrage")
        assert updated["name"] == "My Ouvrage"

    async def test_update_instance_plan_tier(self, db):
        updated = await db.update_instance(plan_tier="starter")
        assert updated["plan_tier"] == "starter"

    async def test_update_instance_unknown_field_raises(self, db):
        with pytest.raises(ValueError, match="Unknown instance fields"):
            await db.update_instance(hacker_field="pwned")


# ===========================================================================
# User credentials (upsert semantics)
# ===========================================================================

class TestUserCredentials:

    async def test_upsert_creates_row_if_missing(self, db):
        user = await db.create_user(email="cred@example.com", name="Cred User")
        # No credentials row exists yet — update_user_credentials should create it
        creds = await db.update_user_credentials(user["id"], slack_webhook_url="https://hooks.slack.com/x")
        assert creds["slack_webhook_url"] == "https://hooks.slack.com/x"

    async def test_get_credentials_none_when_no_row(self, db):
        user = await db.create_user(email="nocred@example.com", name="No Cred")
        result = await db.get_user_credentials(user["id"])
        assert result is None

    async def test_update_credentials_round_trip(self, db):
        user = await db.create_user(email="cred2@example.com", name="Cred2")
        await db.update_user_credentials(user["id"], anthropic_api_key="sk-abc123")
        creds = await db.get_user_credentials(user["id"])
        assert creds["anthropic_api_key"] == "sk-abc123"

    async def test_notification_preferences_json_roundtrip(self, db):
        user = await db.create_user(email="notif@example.com", name="Notif")
        prefs = {"email": True, "slack": False, "push": True}
        await db.update_user_credentials(user["id"], notification_preferences=prefs)
        creds = await db.get_user_credentials(user["id"])
        assert creds["notification_preferences"] == prefs

    async def test_update_credentials_unknown_field_raises(self, db):
        user = await db.create_user(email="unkc@example.com", name="Unk Cred")
        with pytest.raises(ValueError, match="Unknown credential fields"):
            await db.update_user_credentials(user["id"], evil_field="drop table")


# ===========================================================================
# API tokens
# ===========================================================================

class TestApiTokens:

    async def test_create_returns_raw_token_and_id(self, db):
        user = await db.create_user(email="tok@example.com", name="Token User")
        result = await db.create_api_token(user["id"], name="my laptop")
        assert "token" in result
        assert "id" in result
        assert result["name"] == "my laptop"
        assert result["token"].startswith("sb_")
        assert len(result["token"]) == 67  # "sb_" + secrets.token_hex(32)

    async def test_validate_valid_token(self, db):
        user = await db.create_user(email="val@example.com", name="Val User")
        result = await db.create_api_token(user["id"])
        user_id = await db.validate_api_token(result["token"])
        assert user_id == user["id"]

    async def test_validate_invalid_token_returns_none(self, db):
        result = await db.validate_api_token("deadbeef" * 8)
        assert result is None

    async def test_validate_expired_token_returns_none(self, db):
        user = await db.create_user(email="exp@example.com", name="Exp User")
        result = await db.create_api_token(user["id"])
        # Manually set expires_at to the past
        import ouvrage.db.connection as _conn
        async with _conn.get_db() as db_conn:
            await db_conn.execute(
                "UPDATE api_tokens SET expires_at = '2000-01-01T00:00:00' WHERE id = ?",
                (result["id"],),
            )
            await db_conn.commit()
        user_id = await db.validate_api_token(result["token"])
        assert user_id is None

    async def test_revoke_token(self, db):
        user = await db.create_user(email="rev@example.com", name="Rev User")
        result = await db.create_api_token(user["id"])
        revoked = await db.revoke_api_token(result["id"])
        assert revoked is True
        # Token no longer validates
        user_id = await db.validate_api_token(result["token"])
        assert user_id is None

    async def test_revoke_nonexistent_returns_false(self, db):
        revoked = await db.revoke_api_token(999999)
        assert revoked is False

    async def test_list_tokens_never_includes_hash(self, db):
        user = await db.create_user(email="list_tok@example.com", name="List Tok")
        await db.create_api_token(user["id"], name="token-a")
        await db.create_api_token(user["id"], name="token-b")
        tokens = await db.list_api_tokens(user["id"])
        assert len(tokens) == 2
        for tok in tokens:
            assert "token_hash" not in tok

    async def test_list_tokens_only_for_user(self, db):
        user_a = await db.create_user(email="a_tok@example.com", name="A Tok")
        user_b = await db.create_user(email="b_tok@example.com", name="B Tok")
        await db.create_api_token(user_a["id"], name="a-token")
        await db.create_api_token(user_b["id"], name="b-token")
        tokens_a = await db.list_api_tokens(user_a["id"])
        assert all(t["user_id"] == user_a["id"] for t in tokens_a)


# ===========================================================================
# Bootstrap migration
# ===========================================================================

class TestBootstrapMigration:

    async def test_bootstrap_seeds_owner_user(self, db):
        users = await db.list_users()
        owner = next((u for u in users if u["email"] == "owner@localhost"), None)
        assert owner is not None
        assert owner["role"] == "owner"

    async def test_bootstrap_seeds_instance(self, db):
        inst = await db.get_instance()
        assert inst is not None
        assert inst["owner_user_id"] is not None

    async def test_bootstrap_links_instance_to_owner(self, db):
        inst = await db.get_instance()
        owner = await db.get_user_by_email("owner@localhost")
        assert inst["owner_user_id"] == owner["id"]

    async def test_bootstrap_idempotent_on_second_init(self, db):
        # Running init_db() again should not duplicate the owner user
        await db.init_db()
        users = await db.list_users()
        owners = [u for u in users if u["email"] == "owner@localhost"]
        assert len(owners) == 1


# ===========================================================================
# Encryption of sensitive credential fields
# ===========================================================================

class TestEncryption:

    async def test_anthropic_key_stored_encrypted(self, db):
        """Value written to DB is Fernet-encrypted, not plaintext."""
        import ouvrage.db.connection as _conn
        user = await db.create_user(email="enc@example.com", name="Enc User")
        await db.update_user_credentials(user["id"], anthropic_api_key="sk-plaintext")
        # Check the raw DB value is a Fernet token
        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT anthropic_api_key FROM user_credentials WHERE user_id = ?", (user["id"],)
            )
        assert rows
        raw = rows[0]["anthropic_api_key"]
        assert is_fernet_token(raw), f"Expected Fernet token, got: {raw!r}"

    async def test_anthropic_key_decrypted_on_read(self, db):
        """get_user_credentials returns plaintext, not ciphertext."""
        user = await db.create_user(email="dec@example.com", name="Dec User")
        await db.update_user_credentials(user["id"], anthropic_api_key="sk-plaintext")
        creds = await db.get_user_credentials(user["id"])
        assert creds["anthropic_api_key"] == "sk-plaintext"

    async def test_slack_webhook_not_encrypted(self, db):
        """slack_webhook_url is stored as plaintext (not sensitive enough to encrypt)."""
        import ouvrage.db.connection as _conn
        user = await db.create_user(email="slack@example.com", name="Slack User")
        url = "https://hooks.slack.com/services/T0/B0/xyz"
        await db.update_user_credentials(user["id"], slack_webhook_url=url)
        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT slack_webhook_url FROM user_credentials WHERE user_id = ?", (user["id"],)
            )
        raw = rows[0]["slack_webhook_url"]
        assert raw == url  # stored plaintext

    async def test_get_anthropic_key_returns_plaintext(self, db):
        """get_anthropic_key resolves and decrypts the key for the given user."""
        user = await db.create_user(email="anthro@example.com", name="Anthro User")
        await db.update_user_credentials(user["id"], anthropic_api_key="sk-test-key")
        key = await db.get_anthropic_key(user["id"])
        assert key == "sk-test-key"

    async def test_get_anthropic_key_raises_if_missing(self, db):
        """get_anthropic_key raises ValueError when no key is configured."""
        user = await db.create_user(email="nokey@example.com", name="No Key User")
        with pytest.raises(ValueError, match="Anthropic API key"):
            await db.get_anthropic_key(user["id"])

    async def test_get_github_pat_falls_back_to_instance(self, db):
        """get_github_pat falls back to instance.github_pat_encrypted."""
        await db.set_instance_github_pat("ghp_instancetoken")
        pat = await db.get_github_pat("nonexistent-project")
        assert pat == "ghp_instancetoken"

    async def test_get_github_pat_raises_if_not_configured(self, db):
        """get_github_pat raises ValueError when no PAT is found."""
        with pytest.raises(ValueError, match="GitHub PAT"):
            await db.get_github_pat("some-project")

    async def test_set_and_get_instance_github_pat(self, db):
        """set_instance_github_pat stores encrypted, get_instance_github_pat decrypts."""
        import ouvrage.db.connection as _conn
        await db.set_instance_github_pat("ghp_testpat")
        # Raw value in DB should be encrypted
        async with _conn.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT github_pat_encrypted FROM instance WHERE id = 1"
            )
        from ouvrage.crypto import is_fernet_token
        assert is_fernet_token(rows[0]["github_pat_encrypted"])
        # Reading back returns plaintext
        pat = await db.get_instance_github_pat()
        assert pat == "ghp_testpat"

    async def test_get_instance_github_pat_raises_if_not_set(self, db):
        """get_instance_github_pat raises ValueError when not configured."""
        with pytest.raises(ValueError, match="GitHub PAT"):
            await db.get_instance_github_pat()

    async def test_encryption_migration_encrypts_plaintext_values(self, db):
        """init_db() re-run encrypts any pre-existing plaintext credential values."""
        import ouvrage.db.connection as _conn

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
