"""User, instance, credentials, and API token CRUD."""
import hashlib
import json
import secrets

from switchboard.crypto import decrypt_value, encrypt_value, is_fernet_token
from switchboard.db.connection import get_db
from switchboard.db._helpers import now_iso

# Fields in user_credentials that are encrypted at rest
_ENCRYPTED_CREDENTIAL_FIELDS = frozenset({"anthropic_api_key", "github_pat"})

# Field allowlists to prevent SQL injection in dynamic UPDATE queries
_USER_MUTABLE_FIELDS = frozenset({
    "email", "name", "password_hash", "role", "timezone", "updated_at",
})
_INSTANCE_MUTABLE_FIELDS = frozenset({
    "name", "slug", "stripe_customer_id", "plan_tier", "owner_user_id",
})
_CREDENTIALS_MUTABLE_FIELDS = frozenset({
    "anthropic_api_key", "github_pat", "slack_webhook_url",
    "notification_preferences", "updated_at",
})


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def create_user(
    email: str,
    name: str,
    role: str = "member",
    timezone: str = "America/Toronto",
    password_hash: str | None = None,
) -> dict:
    async with get_db() as db:
        ts = now_iso()
        cursor = await db.execute(
            """INSERT INTO users (email, name, password_hash, role, timezone, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (email, name, password_hash, role, timezone, ts, ts),
        )
        await db.commit()
        user_id = cursor.lastrowid
        return {
            "id": user_id, "email": email, "name": name,
            "role": role, "timezone": timezone,
            "created_at": ts, "updated_at": ts,
        }


_USER_PUBLIC_COLS = "id, email, name, role, timezone, created_at, updated_at"


async def get_user(user_id: int) -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_USER_PUBLIC_COLS} FROM users WHERE id = ?", (user_id,)
        )
        return dict(rows[0]) if rows else None


async def get_user_by_email(email: str) -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_USER_PUBLIC_COLS} FROM users WHERE email = ?", (email,)
        )
        return dict(rows[0]) if rows else None


async def update_user(user_id: int, **fields) -> dict:
    unknown = set(fields) - _USER_MUTABLE_FIELDS
    if unknown:
        raise ValueError(f"Unknown user fields: {unknown}")

    fields["updated_at"] = now_iso()
    async with get_db() as db:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [user_id]
        await db.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM users WHERE id = ?", (user_id,))
        if not rows:
            raise ValueError(f"User {user_id} not found")
        return dict(rows[0])


async def list_users() -> list[dict]:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM users ORDER BY created_at ASC"
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Instance (always exactly one row)
# ---------------------------------------------------------------------------

async def get_instance() -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall("SELECT * FROM instance LIMIT 1")
        return dict(rows[0]) if rows else None


async def update_instance(**fields) -> dict:
    unknown = set(fields) - _INSTANCE_MUTABLE_FIELDS
    if unknown:
        raise ValueError(f"Unknown instance fields: {unknown}")

    async with get_db() as db:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values())
        await db.execute(f"UPDATE instance SET {set_clause} WHERE id = 1", values)
        await db.commit()
        rows = await db.execute_fetchall("SELECT * FROM instance WHERE id = 1")
        if not rows:
            raise ValueError("Instance row not found")
        return dict(rows[0])


# ---------------------------------------------------------------------------
# User credentials (1:1 with users)
# ---------------------------------------------------------------------------

async def get_user_credentials(user_id: int) -> dict | None:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM user_credentials WHERE user_id = ?", (user_id,)
        )
        if not rows:
            return None
        cred = dict(rows[0])
        if cred.get("notification_preferences"):
            try:
                cred["notification_preferences"] = json.loads(cred["notification_preferences"])
            except (json.JSONDecodeError, TypeError):
                pass
        # Decrypt sensitive fields
        for field in _ENCRYPTED_CREDENTIAL_FIELDS:
            if cred.get(field) and is_fernet_token(cred[field]):
                cred[field] = decrypt_value(cred[field])
        return cred


async def update_user_credentials(user_id: int, **fields) -> dict:
    unknown = set(fields) - _CREDENTIALS_MUTABLE_FIELDS
    if unknown:
        raise ValueError(f"Unknown credential fields: {unknown}")

    if "notification_preferences" in fields and isinstance(fields["notification_preferences"], dict):
        fields["notification_preferences"] = json.dumps(fields["notification_preferences"])

    # Encrypt sensitive fields before writing
    for field in _ENCRYPTED_CREDENTIAL_FIELDS:
        if field in fields and fields[field] is not None and not is_fernet_token(fields[field]):
            fields[field] = encrypt_value(fields[field])

    fields["updated_at"] = now_iso()

    async with get_db() as db:
        # Upsert: create the row if it doesn't exist
        await db.execute(
            """INSERT OR IGNORE INTO user_credentials (user_id, notification_preferences, updated_at)
               VALUES (?, '{}', ?)""",
            (user_id, now_iso()),
        )
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [user_id]
        await db.execute(
            f"UPDATE user_credentials SET {set_clause} WHERE user_id = ?", values
        )
        await db.commit()
        return await get_user_credentials(user_id)


# ---------------------------------------------------------------------------
# API tokens
# ---------------------------------------------------------------------------

async def create_api_token(user_id: int, name: str | None = None) -> dict:
    """Generate a raw token, store its hash, return {token, id}.

    The raw token is returned exactly once and never stored. Callers must
    present the raw token to validate_api_token().
    """
    raw_token = secrets.token_hex(32)  # 64-char hex string
    token_hash = _hash_token(raw_token)
    ts = now_iso()

    async with get_db() as db:
        cursor = await db.execute(
            """INSERT INTO api_tokens (user_id, token_hash, name, created_at)
               VALUES (?, ?, ?, ?)""",
            (user_id, token_hash, name, ts),
        )
        await db.commit()
        token_id = cursor.lastrowid

    return {"token": raw_token, "id": token_id}


async def validate_api_token(raw_token: str) -> int | None:
    """Return user_id if the token is valid and not expired, else None."""
    token_hash = _hash_token(raw_token)
    ts = now_iso()

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT id, user_id, expires_at FROM api_tokens
               WHERE token_hash = ?""",
            (token_hash,),
        )
        if not rows:
            return None
        row = rows[0]

        # Check expiry
        if row["expires_at"] and row["expires_at"] < ts:
            return None

        # Update last_used_at
        await db.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
            (ts, row["id"]),
        )
        await db.commit()
        return row["user_id"]


async def revoke_api_token(token_id: int) -> bool:
    """Delete the token. Returns True if a row was deleted."""
    async with get_db() as db:
        cursor = await db.execute(
            "DELETE FROM api_tokens WHERE id = ?", (token_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def list_api_tokens(user_id: int) -> list[dict]:
    """List tokens for a user. Never returns token_hash."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT id, user_id, name, last_used_at, created_at, expires_at
               FROM api_tokens WHERE user_id = ?
               ORDER BY created_at DESC""",
            (user_id,),
        )
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Credential resolution — project override → user default → error
# ---------------------------------------------------------------------------

async def get_github_pat(project_id: str) -> str:
    """Resolve GitHub PAT: project.github_pat_override → owner's github_pat → error.

    Decrypts the value before returning.
    """
    from switchboard.db.projects import get_project
    from switchboard.crypto import decrypt_value, is_fernet_token

    project = await get_project(project_id)
    if project and project.get("github_pat_override"):
        override = project["github_pat_override"]
        return decrypt_value(override) if is_fernet_token(override) else override

    instance = await get_instance()
    if not instance:
        raise ValueError("No GitHub PAT configured. Add one in settings or on the project.")
    creds = await get_user_credentials(instance["owner_user_id"])
    if creds and creds.get("github_pat"):
        return creds["github_pat"]  # already decrypted by get_user_credentials
    raise ValueError("No GitHub PAT configured. Add one in settings or on the project.")


async def get_anthropic_key(user_id: int) -> str:
    """Resolve Anthropic API key for a user → error if not configured.

    Decrypts the value before returning.
    """
    creds = await get_user_credentials(user_id)
    if creds and creds.get("anthropic_api_key"):
        return creds["anthropic_api_key"]  # already decrypted by get_user_credentials
    raise ValueError("No Anthropic API key configured.")
