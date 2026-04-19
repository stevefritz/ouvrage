"""ouvrage.dispatch.queue — FIFO task queue drain.

Handles concurrency-limited dispatch: when a slot opens up (on task
completion or cancellation), _drain_queue() dispatches the oldest
eligible queued task.

Also handles project-limit unblocking: when max_projects increases,
_drain_project_limit_blocked() dispatches ready tasks that were blocked.

Lazy import from ouvrage.dispatch.engine (to break circular dependency):
  dispatch_task
"""

import logging

import ouvrage.db as db

log = logging.getLogger(__name__)


async def _drain_queue() -> None:
    """Dispatch the oldest eligible queued task if a concurrency slot is available."""
    from ouvrage.dispatch.lifecycle import lifecycle

    active = await db.count_active_tasks()
    limit = await db.get_concurrency_limit()
    if active >= limit:
        return

    queued = await db.get_queued_tasks()
    if not queued:
        return

    task = queued[0]  # FIFO — oldest first
    log.info(f"Queue drain: dispatching {task['id']} (queued_at={task['queued_at']})")
    try:
        await lifecycle.execute(
            task["id"], "dispatch",
            triggered_by="queue-drain",
            source_detail="_drain_queue (FIFO)",
        )
    except Exception as e:
        log.error(f"Queue drain failed for {task['id']}: {e}")


async def _drain_project_limit_blocked() -> None:
    """Dispatch ready tasks that were blocked by project limit.

    Called when max_projects is updated via /internal/config, in case the
    new limit allows previously-blocked tasks to run.
    """
    from ouvrage.dispatch.internals import is_over_project_limit
    from ouvrage.dispatch.lifecycle import lifecycle

    over_limit, _, _ = await is_over_project_limit()
    if over_limit:
        return

    tasks = await db.get_project_limit_blocked_tasks()
    for task in tasks:
        log.info(f"Project limit drain: dispatching {task['id']}")
        try:
            await lifecycle.execute(
                task["id"], "dispatch",
                triggered_by="project-limit-drain",
                source_detail="_drain_project_limit_blocked",
            )
        except Exception as e:
            log.error(f"Project limit drain failed for {task['id']}: {e}")
