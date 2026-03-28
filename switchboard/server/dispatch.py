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
    _handle_search_conversations,
    _handle_search_message_chunks,
)
from switchboard.server.handlers.projects import (
    _handle_create_project,
    _handle_get_project,
    _handle_update_project,
    _handle_list_projects,
    _handle_pause_project,
    _handle_resume_project,
    _handle_stop_project,
)
from switchboard.server.handlers.tasks import (
    _handle_dispatch_task,
    _handle_release_worktree,
    _handle_resume_task,
    _handle_retry_task,
    _handle_reopen_task,
    _handle_start_reopened_task,
    _handle_cancel_task,
    _handle_approve_task,
    _handle_close_task,
    _handle_get_task_status,
    _handle_list_tasks,
    _handle_update_task,
    _handle_bulk_update_tasks,
    _handle_move_task,
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
    _handle_search_task_messages,
)
from switchboard.server.handlers.components import (
    _handle_create_component,
    _handle_update_component,
    _handle_get_component,
    _handle_list_components,
    _handle_link_conversation,
    _handle_unlink_conversation,
    _handle_search_component,
    _handle_pause_component,
    _handle_resume_component,
    _handle_stop_component,
)
from switchboard.server.handlers.punchlist import (
    _handle_add_punchlist_item,
    _handle_list_punchlist,
    _handle_claim_punchlist_item,
    _handle_resolve_punchlist_item,
)
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
    "search_conversations": _handle_search_conversations,
    "search_message_chunks": _handle_search_message_chunks,
    # Project tools
    "create_project": _handle_create_project,
    "get_project": _handle_get_project,
    "update_project": _handle_update_project,
    "list_projects": _handle_list_projects,
    # Task tools
    "dispatch_task": _handle_dispatch_task,
    "release_worktree": _handle_release_worktree,
    "resume_task": _handle_resume_task,
    "retry_task": _handle_retry_task,
    "reopen_task": _handle_reopen_task,
    "start_reopened_task": _handle_start_reopened_task,
    "cancel_task": _handle_cancel_task,
    "approve_task": _handle_approve_task,
    "close_task": _handle_close_task,
    "get_task_status": _handle_get_task_status,
    "list_tasks": _handle_list_tasks,
    "update_task": _handle_update_task,
    "bulk_update_tasks": _handle_bulk_update_tasks,
    "move_task": _handle_move_task,
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
    # Component tools
    "create_component": _handle_create_component,
    "update_component": _handle_update_component,
    "get_component": _handle_get_component,
    "list_components": _handle_list_components,
    "link_conversation": _handle_link_conversation,
    "unlink_conversation": _handle_unlink_conversation,
    # Punchlist tools
    "add_punchlist_item": _handle_add_punchlist_item,
    "list_punchlist": _handle_list_punchlist,
    "claim_punchlist_item": _handle_claim_punchlist_item,
    "resolve_punchlist_item": _handle_resolve_punchlist_item,
    # Pause/Stop/Resume
    "pause_component": _handle_pause_component,
    "resume_component": _handle_resume_component,
    "stop_component": _handle_stop_component,
    "pause_project": _handle_pause_project,
    "resume_project": _handle_resume_project,
    "stop_project": _handle_stop_project,
    # Ops tools
    "get_context": _handle_get_context,
    "get_guide": _handle_get_guide,
    "search_component": _handle_search_component,
    # Token management
    "create_api_token": _handle_create_api_token,
    "list_api_tokens": _handle_list_api_tokens,
    "revoke_api_token": _handle_revoke_api_token,
    # Files
    "list_files": _handle_list_files,
    "add_task_file": _handle_add_task_file,
}


async def _dispatch_tool(name: str, arguments: dict):
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        raise ValueError(f"Unknown tool: {name}")
    return await handler(arguments)
