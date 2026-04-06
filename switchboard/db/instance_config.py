"""Instance configuration stored in the database.

Provides a single-row `instance_config` table that lets the control plane
push runtime settings (concurrency_limit, max_projects) that override
env-var defaults.
"""

from switchboard.config.constants import DEFAULT_MAX_CONCURRENT
from switchboard.config.settings import MAX_PROJECTS as _MAX_PROJECTS_ENV
from switchboard.db.connection import get_db


async def get_instance_config() -> dict:
    """Return the current instance config row.

    Returns a dict with keys `concurrency_limit`, `max_projects`, and `trial_ends_at`.
    Any value may be None (meaning "use the default" or "not set").
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT concurrency_limit, max_projects, trial_ends_at FROM instance_config WHERE id = 1"
        )
        if rows:
            return {"concurrency_limit": rows[0]["concurrency_limit"],
                    "max_projects": rows[0]["max_projects"],
                    "trial_ends_at": rows[0]["trial_ends_at"]}
        return {"concurrency_limit": None, "max_projects": None, "trial_ends_at": None}


async def set_instance_config(
    concurrency_limit: int | None = None,
    max_projects: int | None = None,
    trial_ends_at: str | None = None,
) -> dict:
    """Upsert the instance config row with the given values.

    Only updates the provided fields; None means "clear the override".
    Returns the resulting config dict (same shape as get_instance_config).
    """
    async with get_db() as db:
        await db.execute(
            """INSERT INTO instance_config (id, concurrency_limit, max_projects, trial_ends_at)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   concurrency_limit = excluded.concurrency_limit,
                   max_projects = excluded.max_projects,
                   trial_ends_at = excluded.trial_ends_at""",
            (concurrency_limit, max_projects, trial_ends_at),
        )
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT concurrency_limit, max_projects, trial_ends_at FROM instance_config WHERE id = 1"
        )
        row = rows[0]
        return {"concurrency_limit": row["concurrency_limit"],
                "max_projects": row["max_projects"],
                "trial_ends_at": row["trial_ends_at"]}


async def get_max_projects() -> int:
    """Return the effective max projects limit.

    Read order:
    1. DB runtime config (set by /internal/config) — if present, use it
    2. MAX_PROJECTS env var
    3. 0 (unlimited)
    """
    cfg = await get_instance_config()
    db_val = cfg.get("max_projects")
    if db_val is not None:
        return int(db_val)
    return _MAX_PROJECTS_ENV


async def get_concurrency_limit() -> int:
    """Return the effective concurrency limit.

    Uses the DB-stored value if set; falls back to DEFAULT_MAX_CONCURRENT.
    """
    cfg = await get_instance_config()
    limit = cfg.get("concurrency_limit")
    if limit is not None:
        return int(limit)
    return DEFAULT_MAX_CONCURRENT
