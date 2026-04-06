"""Tool routing — maps tool names to handler functions."""

from switchboard.server.handlers.conversations import (
    _handle_board,
    _handle_create_conversation,
    _handle_post,
    _handle_read,
    _handle_get_pinned,
    _handle_pin,
    _handle_conversations,
    _handle_archive,
)
from switchboard.server.handlers.projects import (
    _handle_create_project,
    _handle_get_project,
    _handle_update_project,
    _handle_list_projects,
    _handle_pause_project,
    _handle_resume_project,
    _handle_stop_project,
    _handle_delete_project,
)
from switchboard.server.handlers.tasks import (
    _handle_dispatch_task,
    _handle_release_worktree,
    _handle_transition_task,
    _handle_get_task_status,
    _handle_list_tasks,
    _handle_update_task,
    _handle_bulk_update_tasks,
    _handle_update_task_checklist,
    _handle_update_task_phase,
    _handle_post_task_message,
    _handle_read_task_messages,
    _handle_get_session_log,
    _handle_get_dispatch_log,
    _handle_list_attempts,
    _handle_add_checklist_item,
    _handle_remove_checklist_item,
    _handle_update_checklist_item_text,
    _handle_get_pipeline,
    _handle_escalate,
    _handle_search_task_messages,
)
from switchboard.server.handlers.search import _handle_search
from switchboard.server.handlers.invalidations import _handle_invalidate
from switchboard.server.handlers.ops import (
    _handle_get_context,
    _handle_get_guide,
)
from switchboard.server.handlers.tokens import (
    _handle_create_api_token,
    _handle_list_api_tokens,
    _handle_revoke_api_token,
)
from switchboard.git.files import (
    _handle_list_task_files,
    _handle_get_task_file,
)
from switchboard.server.handlers.files_handler import (
    _handle_list_files,
    _handle_add_task_file,
    _handle_add_project_file,
    _handle_get_file,
    _handle_promote_task_file,
)

TOOL_HANDLERS = {
    # Conversation tools
    "board": _handle_board,
    "create_conversation": _handle_create_conversation,
    "post": _handle_post,
    "read": _handle_read,
    "get_pinned": _handle_get_pinned,
    "pin": _handle_pin,
    "conversations": _handle_conversations,
    "archive": _handle_archive,
    # Project tools
    "create_project": _handle_create_project,
    "get_project": _handle_get_project,
    "update_project": _handle_update_project,
    "list_projects": _handle_list_projects,
    "delete_project": _handle_delete_project,
    # Task tools
    "dispatch_task": _handle_dispatch_task,
    "release_worktree": _handle_release_worktree,
    "transition_task": _handle_transition_task,
    "get_task_status": _handle_get_task_status,
    "list_tasks": _handle_list_tasks,
    "update_task": _handle_update_task,
    "bulk_update_tasks": _handle_bulk_update_tasks,
    "list_task_files": _handle_list_task_files,
    "get_task_file": _handle_get_task_file,
    "update_task_checklist": _handle_update_task_checklist,
    "update_task_phase": _handle_update_task_phase,
    "post_task_message": _handle_post_task_message,
    "read_task_messages": _handle_read_task_messages,
    "get_session_log": _handle_get_session_log,
    "get_dispatch_log": _handle_get_dispatch_log,
    "list_attempts": _handle_list_attempts,
    "add_checklist_item": _handle_add_checklist_item,
    "remove_checklist_item": _handle_remove_checklist_item,
    "update_checklist_item": _handle_update_checklist_item_text,
    "get_pipeline": _handle_get_pipeline,
    "search_task_messages": _handle_search_task_messages,
    # Pause/Stop/Resume
    "pause_project": _handle_pause_project,
    "resume_project": _handle_resume_project,
    "stop_project": _handle_stop_project,
    # Ops tools
    "get_context": _handle_get_context,
    "get_guide": _handle_get_guide,
    # Search
    "search": _handle_search,
    "invalidate": _handle_invalidate,
    # Token management
    "create_api_token": _handle_create_api_token,
    "list_api_tokens": _handle_list_api_tokens,
    "revoke_api_token": _handle_revoke_api_token,
    # Files
    "list_files": _handle_list_files,
    "get_attached_file": _handle_get_file,  # deprecated alias for get_file
    "add_task_file": _handle_add_task_file,
    "add_project_file": _handle_add_project_file,
    "get_file": _handle_get_file,
    "promote_task_file": _handle_promote_task_file,
    # Worker-only tools
    "escalate": _handle_escalate,
}


async def _dispatch_tool(name: str, arguments: dict):
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        raise ValueError(f"Unknown tool: {name}")
    return await handler(arguments)
