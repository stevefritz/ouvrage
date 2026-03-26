"""Component dataclass."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Component:
    """Typed representation of a row from the components table."""

    # Required fields
    id: str
    project_id: str
    name: str

    # Description and phase
    description: Optional[str] = None
    phase: str = "planning"

    # Branch and commands (override project defaults)
    base_branch: Optional[str] = None
    setup_command: Optional[str] = None
    test_command: Optional[str] = None

    # Model selection
    model: Optional[str] = None
    review_model: Optional[str] = None

    # Gate automation
    auto_test: Optional[bool] = None
    auto_review: Optional[bool] = None
    max_test_retries: Optional[int] = None
    max_review_retries: Optional[int] = None

    # PR automation
    auto_pr: Optional[bool] = None
    auto_merge: Optional[bool] = None

    # Resource limits
    max_turns: Optional[int] = None
    max_wall_clock: Optional[int] = None

    # Configuration (stored as JSON strings in DB)
    env_overrides: Optional[str] = None   # JSON dict
    secrets: Optional[str] = None         # JSON dict
    review_ignore_patterns: Optional[str] = None  # JSON array

    # State
    paused: bool = False

    # Timestamps
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
