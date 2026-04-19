"""Git operations package — worktree management, branch ops, file access."""

from ouvrage.git.worktree import (
    setup_worktree,
    cleanup_worktree,
    run_setup_command,
    _get_worker_ids,
    _run_as_worker,
    _find_branch_holder,
)
from ouvrage.git.operations import (
    resolve_branch_target,
    parse_repo_url,
    _resolve_push_url,
    _classify_push_error,
    _git_fetch_and_rebase,
    _sync_branch_with_base,
    _ensure_branch_pushed,
    _get_branch_diff,
    _filter_diff_by_ignore_patterns,
    _maybe_create_pr,
    _perform_auto_merge,
)
from ouvrage.git.files import (
    _handle_list_task_files,
    _handle_get_task_file,
    _git_run,
    _resolve_git_ref,
    _is_binary,
    _validate_path,
)

__all__ = [
    # worktree
    "setup_worktree",
    "cleanup_worktree",
    "run_setup_command",
    "_get_worker_ids",
    "_run_as_worker",
    "_find_branch_holder",
    # operations
    "resolve_branch_target",
    "parse_repo_url",
    "_resolve_push_url",
    "_classify_push_error",
    "_git_fetch_and_rebase",
    "_sync_branch_with_base",
    "_ensure_branch_pushed",
    "_get_branch_diff",
    "_filter_diff_by_ignore_patterns",
    "_maybe_create_pr",
    "_perform_auto_merge",
    # files
    "_handle_list_task_files",
    "_handle_get_task_file",
    "_git_run",
    "_resolve_git_ref",
    "_is_binary",
    "_validate_path",
]
