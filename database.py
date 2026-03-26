"""database.py — compatibility shim.

All implementation has moved to switchboard/db/.
This module re-exports everything so existing callers (import database as db)
continue to work without changes.
"""
from switchboard.db import *  # noqa: F401, F403
from switchboard.db import (  # noqa: F401 — explicit re-export of private names
    _get_shared_connection,
    _strip_embedding,
    _read_messages,
    _list_with_aggregates,
    _make_snippet,
    _determine_attempt_outcome,
)

# Constants that the old database.py imported and callers access via `db.CONSTANT`
from switchboard.config.constants import (  # noqa: F401
    CORE_STATE_DEFINITIONS,
    DEFAULT_MAX_TURNS,
    DEFAULT_MAX_WALL_CLOCK,
    DEFAULT_MAX_CONCURRENT,
)
