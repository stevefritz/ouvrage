"""PunchlistItem dataclass and PunchlistStatus enum."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PunchlistStatus(str, Enum):
    """Valid status values for punchlist items."""
    OPEN = "open"
    CLAIMED = "claimed"
    DONE = "done"


@dataclass
class PunchlistItem:
    """Typed representation of a row from the punchlist table."""

    # Required fields
    component_id: str
    item: str

    # DB primary key (None before insert)
    id: Optional[int] = None

    # State
    status: str = PunchlistStatus.OPEN.value

    # Task links
    claimed_by: Optional[str] = None   # task ID
    resolved_by: Optional[str] = None  # task ID

    # Authorship
    author: Optional[str] = None

    # Timestamps
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None
