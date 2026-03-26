"""Web Push notification dispatch via pywebpush + VAPID."""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import switchboard.db as db

log = logging.getLogger("switchboard.web_push")

from switchboard.config.settings import VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY, VAPID_CLAIM_EMAIL

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="web-push")


def is_enabled() -> bool:
    return bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)


def _send_one(subscription: dict, payload_str: str) -> bool:
    """Send one push notification (sync, runs in thread pool)."""
    try:
        from pywebpush import webpush, WebPushException
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {
                    "p256dh": subscription["p256dh"],
                    "auth": subscription["auth"],
                },
            },
            data=payload_str,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_CLAIM_EMAIL},
        )
        return True
    except Exception as e:
        log.warning(f"Push send failed for {subscription.get('endpoint', '?')[:60]}: {e}")
        return False


async def send_notification(payload: dict) -> int:
    """Send a push notification to all subscriptions. Returns count of successful sends."""
    if not is_enabled():
        return 0
    subscriptions = await db.get_push_subscriptions()
    if not subscriptions:
        return 0

    payload_str = json.dumps(payload)
    loop = asyncio.get_event_loop()

    results = await asyncio.gather(
        *[loop.run_in_executor(_executor, _send_one, sub, payload_str) for sub in subscriptions],
        return_exceptions=True,
    )
    return sum(1 for r in results if r is True)


async def dispatch_notification(event_type: str, task_id: str, title: str, body: str) -> None:
    """Check settings and dispatch a web push notification for the given event type.

    event_type: 'failed' | 'needs_review' | 'completed' | 'question'
    """
    if not is_enabled():
        return
    try:
        settings = await db.get_notification_settings()
        setting_key = f"notify_{event_type}"
        if not settings.get(setting_key, False):
            return

        payload = {
            "title": title,
            "body": body,
            "tag": f"task-{task_id}",
            "data": {"url": f"/foreman#/task/{task_id}"},
        }
        count = await send_notification(payload)
        if count:
            log.debug(f"Sent {event_type} push for {task_id} to {count} subscriber(s)")
    except Exception as e:
        log.warning(f"Web push dispatch error ({event_type} / {task_id}): {e}")
