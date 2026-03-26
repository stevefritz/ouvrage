"""Project dataclass."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Project:
    """Typed representation of a row from the projects table."""

    # Required fields
    id: str
    repo: str
    default_branch: str = "main"
    working_dir: str = ""

    # Commands
    setup_command: Optional[str] = None
    teardown_command: Optional[str] = None
    test_command: Optional[str] = None

    # Configuration (stored as JSON strings in DB)
    env_overrides: Optional[str] = None   # JSON dict
    connectors: Optional[str] = None      # JSON dict
    state_definitions: Optional[str] = None  # JSON dict
    review_ignore_patterns: Optional[str] = None  # JSON array

    # Resource limits
    max_turns: Optional[int] = None
    max_wall_clock: Optional[int] = None

    # Model selection
    model: Optional[str] = None

    # CLAUDE.md path override
    claude_md_path: Optional[str] = None

    # State
    paused: bool = False

    # Timestamps
    created_at: Optional[str] = None
