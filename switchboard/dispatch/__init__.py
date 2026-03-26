"""switchboard.dispatch — task orchestration sub-package.

Exposes the full public interface for task lifecycle management:
  engine     — dispatch, resume, retry, cancel, close, approve, chain ops
  recovery   — orphan/stall detection and auto-recovery
  sdk_session — Claude Agent SDK session management and prompt building
  gates      — test gate, review dispatch, and subtask orchestration
  queue      — FIFO queue drain
"""

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

from switchboard.dispatch.sdk_session import (
    _build_task_prompt,
    _build_resume_prompt,
    _setup_log_dir,
    _open_shared,
    _write_dispatch_log,
    _tail_file,
    _run_sdk_session,
    _log_result,
)

from switchboard.dispatch.gates import (
    _tail_lines,
    _run_subtask,
    _run_test_gate,
    _dispatch_review,
    _process_review_result_inline,
    _process_review_result,
)

from switchboard.dispatch.queue import _drain_queue

from switchboard.dispatch._state import _running_tasks, _active_clients

from switchboard.dispatch.engine import (
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

__all__ = [
    # recovery
    "_is_pid_alive",
    "mark_working_for_recovery",
    "_classify_orphan",
    "_classify_with_dependents",
    "_verify_worktree",
    "_build_recovery_message",
    "recover_orphaned_tasks",
    "_recover_task",
    "_recover_gate_subtask",
    "_recover_with_resume",
    "_recover_with_retry",
    "_recover_single_task",
    "check_stalled_tasks",
    # sdk_session
    "_build_task_prompt",
    "_build_resume_prompt",
    "_setup_log_dir",
    "_open_shared",
    "_write_dispatch_log",
    "_tail_file",
    "_run_sdk_session",
    "_log_result",
    # gates
    "_tail_lines",
    "_run_subtask",
    "_run_test_gate",
    "_dispatch_review",
    "_process_review_result_inline",
    "_process_review_result",
    # queue
    "_drain_queue",
    # engine — public API
    "_running_tasks",
    "_active_clients",
    "_handle_task_exception",
    "_resolve_limit",
    "_check_and_dispatch_dependents",
    "_invalidate_chain",
    "_rebase_and_redispatch",
    "_update_usage",
    "_task_slug",
    "archive_task_logs",
    "list_attempts",
    "_find_archive_path",
    "release_worktree",
    "_auto_release_worktree",
    "dispatch_task",
    "resume_task",
    "retry_task",
    "reopen_task",
    "cancel_reopen",
    "start_reopened_task",
    "cancel_task",
    "skip_gate",
    "advance_chain",
    "cancel_chain",
    "approve_task",
    "close_task",
    "pause_component",
    "resume_component",
    "stop_component",
    "pause_project",
    "resume_project",
    "stop_project",
]
