"""Conversation and Message dataclasses, MessageType enum."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class MessageType(str, Enum):
    """Valid message type values used in both conversation and task messages."""
    SPEC = "spec"
    PLAN = "plan"
    QUESTION = "question"
    ANSWER = "answer"
    NOTE = "note"
    REVIEW = "review"
    STATUS = "status"
    PROGRESS = "progress"
    RESULT = "result"
    TEST_RESULT = "test-result"
    HANDOFF = "handoff"


@dataclass
class Conversation:
    """Typed representation of a row from the conversations table."""

    # Required fields
    id: str
    project: str
    goal: str

    # State
    archived: bool = False

    # External link
    claude_chat_url: Optional[str] = None

    # Timestamps
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class Message:
    """Typed representation of a row from the messages table.

    Used for both conversation messages and task messages.
    Exactly one of conversation_id or task_id is expected to be set.
    """

    # Required fields
    author: str
    content: str

    # DB primary key (None before insert)
    id: Optional[int] = None

    # Link to parent (one of these will be set)
    conversation_id: Optional[str] = None
    task_id: Optional[str] = None

    # Optional metadata
    type: Optional[str] = None
    title: Optional[str] = None
    pinned: bool = False

    # Attempt number (task messages only)
    attempt_number: int = 1

    # Timestamps
    created_at: Optional[str] = None

    # Note: embedding (BLOB) is excluded — internal only, never surfaced via API
