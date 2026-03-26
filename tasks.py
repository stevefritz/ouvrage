"""tasks — compatibility shim.

All logic has been extracted to switchboard/dispatch/*.
This module re-exports everything so existing callers (server.py, tests) continue
to work without modification.

Keeping a few module-level names that tests access directly:
  - asyncio   (tests patch tasks.asyncio.sleep, tasks.asyncio.create_subprocess_exec)
  - db        (tests patch tasks.db.get_task etc. — db is a module singleton)
  - notify    (tests patch tasks.notify.X — notify is a module singleton)
  - _active_clients  (tests write to tasks._active_clients directly)
  - _running_tasks   (exposed for completeness)
"""

import asyncio  # noqa: F401 — tests patch tasks.asyncio.*
import switchboard.db as db  # noqa: F401 — tests patch tasks.db.* (must be switchboard.db so patches propagate to engine.py)
from switchboard.notifications import slack as notify  # noqa: F401 — tests patch tasks.notify.*

# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

from switchboard.dispatch.recovery import (
    _is_pid_alive,
    mark_working_for_recovery,
    _classify_orphan,
    _classify_with_dependents,
    _verify_worktree,
    _build_recovery_message,
    recover_orphaned_tasks,
    _recover_task,
    _recover_gate_subtask,
    _recover_with_resume,
    _recover_with_retry,
    _recover_single_task,
    check_stalled_tasks,
)

# Re-export recovery constants so existing references keep working
from switchboard.config.settings import (
    RECOVERY_STAGGER_SECONDS,
    MAX_RECOVERY_ATTEMPTS,
    RECOVERY_ENABLED,
)
from switchboard.config.constants import (
    STALL_THRESHOLD_SECONDS,
    _DEFAULT_REVIEW_IGNORE_PATTERNS,
    _TAG_REVIEW_GUIDANCE,
    _DEFAULT_REVIEW_GUIDANCE,
)

# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------

from switchboard.config.settings import WORKER_USER
from switchboard.git.worktree import (
    _get_worker_ids,
    _run_as_worker,
    _find_branch_holder,
    setup_worktree,
    cleanup_worktree,
    run_setup_command,
)
from switchboard.git.operations import (
    resolve_branch_target,
    _git_fetch_and_rebase,
    _sync_branch_with_base,
    _ensure_branch_pushed,
    _get_branch_diff,
    _filter_diff_by_ignore_patterns,
    _maybe_create_pr,
    _perform_auto_merge,
)

# ---------------------------------------------------------------------------
# SDK Session + Prompt Building
# ---------------------------------------------------------------------------

from switchboard.dispatch.sdk_session import (
    _build_task_prompt,
    _build_resume_prompt,
    _setup_log_dir,
    _open_shared,
    _write_dispatch_log,
    _tail_file,
    _run_sdk_session,
    _log_result,
    _orig_anyio_open_process,
    _isolated_open_process,
)

# ---------------------------------------------------------------------------
# Gate Pipeline
# ---------------------------------------------------------------------------

from switchboard.dispatch.gates import (
    _tail_lines,
    _run_subtask,
    _run_test_gate,
    _dispatch_review,
    _process_review_result_inline,
    _process_review_result,
)

# ---------------------------------------------------------------------------
# FIFO Queue Drain
# ---------------------------------------------------------------------------

from switchboard.dispatch.queue import _drain_queue

# ---------------------------------------------------------------------------
# Engine — task lifecycle (dispatch, resume, retry, cancel, close, etc.)
# ---------------------------------------------------------------------------

from switchboard.dispatch.engine import (
    _running_tasks,
    _active_clients,
    _handle_task_exception,
    _resolve_limit,
    _check_and_dispatch_dependents,
    _invalidate_chain,
    _rebase_and_redispatch,
    _update_usage,
    _task_slug,
    archive_task_logs,
    list_attempts,
    _find_archive_path,
    release_worktree,
    _auto_release_worktree,
    dispatch_task,
    resume_task,
    retry_task,
    reopen_task,
    cancel_reopen,
    start_reopened_task,
    cancel_task,
    skip_gate,
    advance_chain,
    cancel_chain,
    approve_task,
    close_task,
    pause_component,
    resume_component,
    stop_component,
    pause_project,
    resume_project,
    stop_project,
)
