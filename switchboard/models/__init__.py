"""Typed dataclasses and enums for all core Switchboard entities.

These are data containers only — no behavior, no DB access.
Existing code continues using raw dicts; these models are available for new code.
"""

from switchboard.models.task import Task, TaskStatus, GateStatus
from switchboard.models.project import Project
from switchboard.models.component import Component
from switchboard.models.conversation import Conversation, Message, MessageType
from switchboard.models.checklist import ChecklistItem
from switchboard.models.punchlist import PunchlistItem, PunchlistStatus

__all__ = [
    "Task",
    "TaskStatus",
    "GateStatus",
    "Project",
    "Component",
    "Conversation",
    "Message",
    "MessageType",
    "ChecklistItem",
    "PunchlistItem",
    "PunchlistStatus",
]
