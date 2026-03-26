"""Compatibility shim — server.py now delegates to switchboard/server/.

Tests import directly from this module; the re-exports below maintain
backward compatibility without requiring any test changes.
"""

import asyncio  # noqa: F401 (tests may reference this)
import switchboard.db as db  # noqa: F401 — tests patch server.db.* (must be switchboard.db so patches propagate to handler modules)

# ---------------------------------------------------------------------------
# Handler re-exports (tests do "from server import _handle_*")
# ---------------------------------------------------------------------------

from switchboard.server.handlers.conversations import (  # noqa: F401
    _handle_board,
    _handle_create_conversation,
    _handle_post,
    _handle_read,
    _handle_get_pinned,
    _handle_pin,
    _handle_conversations,
    _handle_archive,
    _handle_search_conversations,
)

from switchboard.server.handlers.projects import (  # noqa: F401
    WORKTREE_BASE,
    _resolve_working_dir,
    _handle_create_project,
    _handle_get_project,
    _handle_update_project,
    _handle_list_projects,
)

from switchboard.server.handlers.tasks import (  # noqa: F401
    _UPDATE_TASK_FIELDS,
    _handle_dispatch_task,
    _handle_release_worktree,
    _handle_resume_task,
    _handle_approve_task,
    _handle_retry_task,
    _handle_reopen_task,
    _handle_start_reopened_task,
    _handle_cancel_task,
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
    _resolve_log_dir,
    _handle_get_session_log,
    _handle_get_dispatch_log,
    _handle_list_attempts,
    _handle_add_checklist_item,
    _handle_remove_checklist_item,
    _handle_update_checklist_item_text,
    _handle_get_pipeline,
    _handle_search_task_messages,
)

from switchboard.server.handlers.components import (  # noqa: F401
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
from switchboard.server.handlers.projects import (  # noqa: F401
    _handle_pause_project,
    _handle_resume_project,
    _handle_stop_project,
)

from switchboard.server.handlers.punchlist import (  # noqa: F401
    _handle_add_punchlist_item,
    _handle_list_punchlist,
    _handle_claim_punchlist_item,
    _handle_resolve_punchlist_item,
)

from switchboard.server.handlers.ops import (  # noqa: F401
    GUIDE_STATIC,
    _handle_get_context,
    _handle_get_guide,
)

from switchboard.server.handlers.common import (  # noqa: F401
    PR_URL_RE,
    _embed_message_async,
)

# Git file access helpers (tests import these from server)
from switchboard.git.files import (  # noqa: F401
    _handle_list_task_files,
    _handle_get_task_file,
    _git_run,
    _resolve_git_ref,
    _is_binary,
    _validate_path,
    _fetch_cache,
    _FETCH_TTL,
)

# Dispatch routing + TOOLS list
from switchboard.server.dispatch import (  # noqa: F401
    TOOL_HANDLERS,
    _dispatch_tool,
)
from switchboard.server.tools import TOOLS  # noqa: F401

# App-level objects (MCP server instance, list/call_tool registrations)
from switchboard.server.app import (  # noqa: F401
    SERVER_INSTRUCTIONS,
    server,
    list_tools,
    call_tool,
    main,
)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
