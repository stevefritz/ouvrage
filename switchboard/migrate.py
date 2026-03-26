"""Auth migration: create owner user from Authelia credentials, seed OAuth client.

Usage:
    python -m switchboard migrate-auth --email X --name Y --password-hash Z --slug W

Auto-migration on startup: set SWITCHBOARD_OWNER_EMAIL + SWITCHBOARD_OWNER_PASSWORD_HASH env vars.
"""

import logging
import os

log = logging.getLogger("switchboard.migrate")


async def run_migrate_auth(
    email: str,
    name: str,
    password_hash: str,
    slug: str,
    instance_name: str = "Switchboard",
) -> dict:
    """Create owner user and seed OAuth client. Idempotent.

    Returns dict with status ('migrated' or 'already_migrated') and owner user info.
    """
    from switchboard.db import init_db
    from switchboard.db.users import (
        get_user_by_email,
        update_user,
        update_instance,
        get_instance,
        create_user,
    )
    from switchboard.db.connection import get_db
    from switchboard.db._helpers import now_iso
    from switchboard.auth import oauth as oauth_server
    from switchboard.crypto import decrypt_value

    # Ensure schema is initialised (creates tables + placeholder owner@localhost if fresh)
    await init_db()

    # --- Idempotency: already migrated? ---
    existing = await get_user_by_email(email)
    if existing:
        log.info("Owner user %s already exists — skipping migration", email)
        return {"status": "already_migrated", "owner_id": existing["id"]}

    # --- Replace or create owner user ---
    placeholder = await get_user_by_email("owner@localhost")
    if placeholder:
        # Upgrade the placeholder created by bootstrap migration.
        # Same user_id → all FK backfills from bootstrap remain valid.
        await update_user(
            placeholder["id"],
            email=email,
            name=name,
            password_hash=password_hash,
            role="owner",
        )
        owner_id = placeholder["id"]
        log.info("Replaced placeholder owner with %s (id=%d)", email, owner_id)
    else:
        # Fresh DB where bootstrap didn't run yet (or was skipped).
        async with get_db() as db:
            ts = now_iso()
            cursor = await db.execute(
                """INSERT INTO users (email, name, password_hash, role, timezone, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (email, name, password_hash, "owner", "America/Toronto", ts, ts),
            )
            owner_id = cursor.lastrowid
            # Create instance if it doesn't exist
            inst_rows = await db.execute_fetchall("SELECT id FROM instance LIMIT 1")
            if not inst_rows:
                await db.execute(
                    """INSERT INTO instance (id, name, slug, plan_tier, owner_user_id, created_at)
                       VALUES (1, ?, ?, ?, ?, ?)""",
                    (instance_name, slug, "free", owner_id, ts),
                )
            await db.commit()
        log.info("Created owner user %s (id=%d)", email, owner_id)

    # --- Update instance name/slug ---
    await update_instance(name=instance_name, slug=slug)

    # --- Backfill any remaining NULL FKs (safety net for rows added after bootstrap) ---
    async with get_db() as db:
        await db.execute(
            "UPDATE projects SET created_by = ? WHERE created_by IS NULL", (owner_id,)
        )
        await db.execute(
            "UPDATE components SET created_by = ? WHERE created_by IS NULL", (owner_id,)
        )
        await db.execute(
            "UPDATE conversations SET created_by = ? WHERE created_by IS NULL", (owner_id,)
        )
        await db.execute(
            "UPDATE tasks SET created_by = ?, dispatched_by = ? WHERE created_by IS NULL",
            (owner_id, owner_id),
        )
        await db.execute(
            """UPDATE messages SET user_id = ?
               WHERE author NOT IN ('dispatcher', 'cc-worker', 'switchboard')
               AND user_id IS NULL""",
            (owner_id,),
        )
        await db.commit()

    # --- Seed OAuth client ---
    oauth_server.init_oauth_keys()
    await oauth_server.seed_default_client()

    # --- Read back + log client credentials prominently ---
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT client_id, client_secret_encrypted FROM oauth_clients WHERE client_id = 'claude-mcp'"
        )
    if rows:
        client_id = rows[0]["client_id"]
        try:
            client_secret = decrypt_value(rows[0]["client_secret_encrypted"])
        except Exception:
            client_secret = "(could not decrypt — check SWITCHBOARD_MASTER_KEY)"
        _print_credentials(client_id, client_secret)
    else:
        log.warning("oauth_clients table is empty after seeding — something went wrong")
        client_id = "claude-mcp"
        client_secret = "(not found)"

    return {
        "status": "migrated",
        "owner_id": owner_id,
        "client_id": client_id,
        "client_secret": client_secret,
    }


def _print_credentials(client_id: str, client_secret: str) -> None:
    """Print OAuth credentials prominently to stdout."""
    border = "=" * 60
    print(border)
    print("  OAUTH CLIENT CREDENTIALS")
    print(border)
    print(f"  client_id:     {client_id}")
    print(f"  client_secret: {client_secret}")
    print(border)
    print("  Save these — you will need them to connect Claude.ai")
    print(border)
