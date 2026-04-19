"""Typed dataclasses and enums for all core Switchboard entities.

These are data containers only — no behavior, no DB access.
Existing code continues using raw dicts; these models are available for new code.
"""

from ouvrage.models.task import Task, TaskStatus, GateStatus
from ouvrage.models.project import Project
from ouvrage.models.component import Component
from ouvrage.models.conversation import Conversation, Message, MessageType
from ouvrage.models.checklist import ChecklistItem
from ouvrage.models.punchlist import PunchlistItem, PunchlistStatus

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
