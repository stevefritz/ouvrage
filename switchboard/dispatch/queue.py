"""switchboard.dispatch.queue — FIFO task queue drain.

Handles concurrency-limited dispatch: when a slot opens up (on task
completion or cancellation), _drain_queue() dispatches the oldest
eligible queued task.

Lazy import from tasks (to break circular dependency):
  dispatch_task
"""

import logging

import database as db

log = logging.getLogger("switchboard.tasks")


async def _drain_queue() -> None:
    """Dispatch the oldest eligible queued task if a concurrency slot is available."""
    from tasks import dispatch_task

    active = await db.count_active_tasks()
    if active >= db.DEFAULT_MAX_CONCURRENT:
        return

    queued = await db.get_queued_tasks()
    if not queued:
        return

    task = queued[0]  # FIFO — oldest first
    log.info(f"Queue drain: dispatching {task['id']} (queued_at={task['queued_at']})")
    try:
        await dispatch_task(
            project_id=task["project_id"],
            task_id=task["id"],
            goal=task["goal"],
            auto_test=task.get("auto_test", True),
        )
    except Exception as e:
        log.error(f"Queue drain failed for {task['id']}: {e}")
