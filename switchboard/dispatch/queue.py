"""switchboard.dispatch.queue — FIFO task queue drain.

Handles concurrency-limited dispatch: when a slot opens up (on task
completion or cancellation), _drain_queue() dispatches the oldest
eligible queued task.

Lazy import from switchboard.dispatch.engine (to break circular dependency):
  dispatch_task
"""

import logging

import switchboard.db as db

log = logging.getLogger(__name__)


async def _drain_queue() -> None:
    """Dispatch the oldest eligible queued task if a concurrency slot is available."""
    from switchboard.dispatch.lifecycle import lifecycle

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
