"""Git credentials CRUD — stores encrypted credentials per provider."""

from switchboard.db.connection import get_db
from switchboard.db._helpers import now_iso


async def create_credential(
    provider: str, credential: str, hostname: str | None = None,
    credential_last4: str | None = None,
) -> dict:
    """Create a git credential. Credential should already be encrypted."""
    # Default hostnames per provider
    default_hostnames = {
        "github": "github.com",
        "gitlab": "gitlab.com",
        "bitbucket": "bitbucket.org",
    }
    if not hostname:
        hostname = default_hostnames.get(provider, provider)

    ts = now_iso()
    async with get_db() as db:
        cursor = await db.execute(
            """INSERT INTO git_credentials (provider, credential, hostname, credential_last4, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (provider, credential, hostname, credential_last4, ts),
        )
        await db.commit()
        return {
            "id": cursor.lastrowid,
            "provider": provider,
            "credential": credential,
            "hostname": hostname,
            "credential_last4": credential_last4,
            "created_at": ts,
        }


async def get_credential_by_provider(provider: str) -> dict | None:
    """Get the credential for a provider. Returns first match."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM git_credentials WHERE provider = ? ORDER BY id LIMIT 1",
            (provider,),
        )
        return dict(rows[0]) if rows else None


async def get_credential_by_hostname(hostname: str) -> dict | None:
    """Get credential matching a hostname. Used by detect_provider."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM git_credentials WHERE hostname = ? ORDER BY id LIMIT 1",
            (hostname.lower(),),
        )
        return dict(rows[0]) if rows else None


async def list_credentials() -> list[dict]:
    """List all git credentials (without decrypting)."""
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM git_credentials ORDER BY provider, id"
        )
        return [dict(r) for r in rows]


async def update_credential(credential_id: int, **fields) -> dict:
    """Update a git credential."""
    allowed = {"provider", "credential", "hostname", "credential_last4"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"Unknown credential fields: {unknown}")

    async with get_db() as db:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [credential_id]
        await db.execute(
            f"UPDATE git_credentials SET {set_clause} WHERE id = ?", values
        )
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT * FROM git_credentials WHERE id = ?", (credential_id,)
        )
        if not rows:
            raise ValueError(f"Credential {credential_id} not found")
        return dict(rows[0])


async def delete_credential(credential_id: int) -> bool:
    """Delete a git credential. Returns True if deleted."""
    async with get_db() as db:
        cursor = await db.execute(
            "DELETE FROM git_credentials WHERE id = ?", (credential_id,)
        )
        await db.commit()
        return cursor.rowcount > 0
