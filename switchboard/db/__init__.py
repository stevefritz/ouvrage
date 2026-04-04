"""switchboard.db — database access layer.

Re-exports every public function so callers can do:
    import switchboard.db as db
    db.get_task(...)
"""

# Connection
from switchboard.db.connection import (
    _get_shared_connection,
    get_db,
    close_db,
)

# Schema
from switchboard.db.schema import init_db

# Helpers (private but some are re-exported for test access)
from switchboard.db._helpers import (
    now_iso,
    _strip_embedding,
    _read_messages,
    _list_with_aggregates,
    _make_snippet,
    _determine_attempt_outcome,
    read_messages_around,
)

# Push / notifications
from switchboard.db.push import (
    get_push_subscriptions,
    save_push_subscription,
    delete_push_subscription,
    get_notification_settings,
    update_notification_settings,
)

# Conversations
from switchboard.db.conversations import (
    create_conversation,
    post_message,
    read_messages,
    get_pinned,
    pin_message,
    board,
    list_conversations,
    archive_conversation,
)

# Projects
from switchboard.db.projects import (
    create_project,
    get_project,
    update_project,
    list_projects,
    count_projects,
    delete_project,
    rename_project,
)

# Tasks
from switchboard.db.tasks import (
    create_task,
    get_task,
    update_task,
    bulk_update_tasks,
    move_task,
    list_tasks,
    get_project_task_counts,
    get_recent_activity,
    get_dependents,
    get_chain,
    count_active_tasks,
    get_working_tasks_for_conversation,
    get_queued_tasks,
    post_task_message,
    read_task_messages,
    get_task_pinned,
    get_message_by_id,
    set_message_embedding,
    get_task_status,
    get_task_attempts,
    get_merged_state_definitions,
    get_state_definition,
    create_checklist_items,
    get_checklist,
    update_checklist_item,
    add_checklist_item,
    remove_checklist_item,
    update_checklist_item_text,
    add_artifact,
    get_artifacts,
    set_task_tags,
    get_task_tags,
    create_subtask,
    update_subtask,
    get_subtasks,
    get_subtask,
    get_tasks_with_open_prs,
    create_attempt,
    update_attempt,
    get_attempt,
    get_previous_attempt_session_id,
)

# Components
from switchboard.db.components import (
    create_component,
    get_component,
    update_component,
    list_components,
    link_conversation,
    unlink_conversation,
    get_component_conversations,
    resolve_config,
)

# Punchlist
from switchboard.db.punchlist import (
    add_punchlist_item,
    get_punchlist_item,
    list_punchlist,
    claim_punchlist_item,
    resolve_punchlist_items_for_task,
    create_punchlist_item,
    update_punchlist_item,
    delete_punchlist_item,
    revert_punchlist_items_for_task,
)

# Constants (re-exported for callers that do `db.DEFAULT_MAX_CONCURRENT` etc.)
from switchboard.config.constants import (
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_TURNS,
    DEFAULT_MAX_WALL_CLOCK,
    CORE_STATE_DEFINITIONS,
    TASK_MUTABLE_FIELDS,
)

# Users, instance, credentials, API tokens
from switchboard.db.users import (
    create_user,
    get_user,
    get_user_by_email,
    get_user_by_email_with_auth,
    update_user,
    list_users,
    get_instance,
    update_instance,
    get_user_credentials,
    update_user_credentials,
    create_api_token,
    validate_api_token,
    revoke_api_token,
    list_api_tokens,
    get_github_pat,
    get_anthropic_key,
    set_instance_github_pat,
    get_instance_github_pat,
)

# Files
from switchboard.db.files import (
    create_file,
    get_file,
    list_files,
    update_file,
    delete_file,
    promote_task_file,
)

# Instance config (control-plane overrides)
from switchboard.db.instance_config import (
    get_instance_config,
    set_instance_config,
    get_concurrency_limit,
    get_max_projects,
)

# Audit
from switchboard.db.audit import (
    write_audit_log,
    get_audit_log,
)

# Search
from switchboard.db.search import (
    search_messages_semantic,
    get_messages_needing_embedding,
    count_messages_needing_embedding,
    get_activity,
    get_component_activity,
    search_task_messages,
    search_component,
    search_conversation_messages,
    index_message_chunks,
    search_message_chunks,
    get_messages_needing_chunking,
    set_task_embedding,
    get_tasks_needing_embedding,
    search_tasks_semantic,
    search_messages_fts,
    search_tasks_fts,
)

__all__ = [
    # connection
    "_get_shared_connection", "get_db", "close_db",
    # schema
    "init_db",
    # helpers
    "now_iso", "_strip_embedding", "_read_messages", "_list_with_aggregates",
    "_make_snippet", "_determine_attempt_outcome", "read_messages_around",
    # push
    "get_push_subscriptions", "save_push_subscription", "delete_push_subscription",
    "get_notification_settings", "update_notification_settings",
    # conversations
    "create_conversation", "post_message", "read_messages", "get_pinned",
    "pin_message", "board", "list_conversations", "archive_conversation",
    # projects
    "create_project", "get_project", "update_project", "list_projects", "count_projects", "delete_project", "rename_project",
    # tasks
    "create_task", "get_task", "update_task", "bulk_update_tasks", "move_task",
    "list_tasks", "get_project_task_counts", "get_recent_activity", "get_dependents",
    "get_chain", "count_active_tasks", "get_working_tasks_for_conversation",
    "get_queued_tasks", "post_task_message", "read_task_messages", "get_task_pinned",
    "set_message_embedding", "get_task_status", "get_task_attempts",
    "get_merged_state_definitions", "get_state_definition",
    "create_checklist_items", "get_checklist", "update_checklist_item",
    "add_checklist_item", "remove_checklist_item", "update_checklist_item_text",
    "add_artifact", "get_artifacts", "set_task_tags", "get_task_tags",
    "create_subtask", "update_subtask", "get_subtasks", "get_subtask",
    "create_attempt", "update_attempt", "get_attempt", "get_previous_attempt_session_id",
    # components
    "create_component", "get_component", "update_component", "list_components",
    "link_conversation", "unlink_conversation", "get_component_conversations",
    "resolve_config",
    # punchlist
    "add_punchlist_item", "get_punchlist_item", "list_punchlist",
    "claim_punchlist_item", "resolve_punchlist_items_for_task",
    "create_punchlist_item", "update_punchlist_item", "delete_punchlist_item",
    "revert_punchlist_items_for_task",
    # users / instance / credentials / api tokens
    "create_user", "get_user", "get_user_by_email", "get_user_by_email_with_auth",
    "update_user", "list_users",
    "get_instance", "update_instance",
    "get_user_credentials", "update_user_credentials",
    "create_api_token", "validate_api_token", "revoke_api_token", "list_api_tokens",
    "get_github_pat", "get_anthropic_key",
    "set_instance_github_pat", "get_instance_github_pat",
    # instance config
    "get_instance_config", "set_instance_config", "get_concurrency_limit", "get_max_projects",
    # audit
    "write_audit_log", "get_audit_log",
    # search
    "search_messages_semantic", "get_messages_needing_embedding",
    "count_messages_needing_embedding", "get_activity", "get_component_activity",
    "search_task_messages", "search_component", "search_conversation_messages",
    "index_message_chunks", "search_message_chunks", "get_messages_needing_chunking",
    "set_task_embedding", "get_tasks_needing_embedding", "search_tasks_semantic",
    "search_messages_fts", "search_tasks_fts",
    # constants
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MAX_WALL_CLOCK",
    "CORE_STATE_DEFINITIONS",
    "TASK_MUTABLE_FIELDS",
]
