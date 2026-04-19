"""ChecklistItem dataclass."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class ChecklistItem:
    """Typed representation of a row from the task_checklist table."""

    # Required fields
    task_id: str
    item: str

    # DB primary key (None before insert)
    id: Optional[int] = None

    # State
    done: bool = False

    # Timestamps
    updated_at: Optional[str] = None
