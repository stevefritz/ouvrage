"""Slack channel notifications for task lifecycle events."""

import logging
import os

import httpx
import web_push

log = logging.getLogger("switchboard.notify")

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")

# Per-task thread timestamps
_task_threads: dict[str, str] = {}  # task_id -> thread_ts


def is_enabled() -> bool:
    return bool(SLACK_BOT_TOKEN and SLACK_CHANNEL_ID)


async def _post(text: str, thread_ts: str | None = None, blocks: list | None = None) -> str | None:
    """Post a message to the Slack channel. Returns message ts."""
    if not SLACK_CHANNEL_ID:
        return None

    payload = {
        "channel": SLACK_CHANNEL_ID,
        "text": text,  # Fallback for push notifications / accessibility
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if blocks:
        payload["blocks"] = blocks

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json=payload,
            )
            data = resp.json()
            if data.get("ok"):
                return data["ts"]
            else:
                log.error(f"Slack chat.postMessage failed: {data.get('error')}")
                return None
    except Exception as e:
        log.warning(f"Slack notification failed: {e}")
        return None


# ── Helpers ───────────────────────────────────────────────────────────

def _progress_bar(done: int, total: int, length: int = 10) -> str:
    """Render a text progress bar."""
    if total == 0:
        return "\u2591" * length
    filled = round(done / total * length)
    return "\u2593" * filled + "\u2591" * (length - filled)


def _checklist_block(checklist: list[dict]) -> dict:
    """Build a Slack section block showing checklist items."""
    lines = []
    for item in checklist:
        icon = "\u2705" if item.get("done") else "\u2b1c"
        lines.append(f"{icon} {item['item']}")
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "\n".join(lines),
        },
    }


# ── Task lifecycle notifications ──────────────────────────────────────

async def task_dispatched(task_id: str, goal: str, project_id: str,
                          checklist_total: int, checklist: list[dict] | None = None,
                          spec: str | None = None, resumed: bool = False):
    """Notify when a task is dispatched. Posts checklist + spec in thread."""
    if not is_enabled():
        return

    action = "Resumed" if resumed else "Dispatched"
    emoji = "\U0001f504" if resumed else "\U0001f680"

    # Build checklist preview for the main message
    checklist_lines = ""
    if checklist:
        for item in checklist[:10]:  # Cap at 10 items in main message
            icon = "\u2705" if item.get("done") else "\u2b1c"
            checklist_lines += f"\n{icon}  {item['item']}"
        if len(checklist) > 10:
            checklist_lines += f"\n_...and {len(checklist) - 10} more_"

    text = f"{emoji} {action}: {task_id} \u2014 {goal}"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{emoji} *{action}:* `{task_id}`\n>{goal}",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"*Project:* `{project_id}`"},
                {"type": "mrkdwn", "text": f"*Checklist:* 0/{checklist_total}"},
            ],
        },
    ]

    if checklist_lines:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": checklist_lines.strip(),
            },
        })

    ts = await _post(text, blocks=blocks)
    if ts:
        _task_threads[task_id] = ts

    # Thread-reply with full spec if provided
    if ts and spec:
        # Truncate spec for Slack's 3000 char block limit
        spec_text = spec[:2900] + "\n_...(truncated)_" if len(spec) > 2900 else spec
        await _post(
            text=f"Spec for {task_id}",
            thread_ts=ts,
            blocks=[
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "Task Spec", "emoji": True},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": spec_text},
                },
            ],
        )


async def task_progress(task_id: str, title: str | None, content: str, msg_type: str = "progress"):
    """Notify on progress/result messages posted by CC."""
    if not is_enabled():
        return

    thread_ts = _task_threads.get(task_id)
    if not thread_ts:
        return

    # Truncate for Slack's block text limit (3000 chars)
    preview = content[:2900] + "\n_...(truncated)_" if len(content) > 2900 else content
    header = title or msg_type.capitalize()

    await _post(
        text=f"{header}\n{content[:500]}",
        thread_ts=thread_ts,
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{header}*"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": preview},
            },
        ],
    )


async def task_phase_changed(task_id: str, phase: str):
    """Notify when CC updates its phase (what it's working on)."""
    if not is_enabled():
        return

    thread_ts = _task_threads.get(task_id)
    if not thread_ts:
        return

    await _post(f":gear: {phase}", thread_ts=thread_ts)


async def task_heartbeat(task_id: str, turns: int, elapsed_s: float,
                         last_tool: str | None = None):
    """Periodic heartbeat so the user knows CC is still alive."""
    if not is_enabled():
        return

    thread_ts = _task_threads.get(task_id)
    if not thread_ts:
        return

    mins = int(elapsed_s // 60)
    secs = int(elapsed_s % 60)
    parts = [f"\U0001f493 Still working \u2014 {turns} turns, {mins}m{secs:02d}s"]
    if last_tool:
        parts.append(f"  _Last tool: {last_tool}_")
    await _post("\n".join(parts), thread_ts=thread_ts)


async def checklist_progress(task_id: str, item_text: str, done: int, total: int):
    """Notify on checklist item completion."""
    if not is_enabled():
        return

    thread_ts = _task_threads.get(task_id)
    if not thread_ts:
        return

    bar = _progress_bar(done, total)
    text = f"`[{bar}]` *{done}/{total}* \u2014 \u2705 {item_text}"
    await _post(text, thread_ts=thread_ts)


async def task_question(task_id: str, question: str):
    """Notify when CC posts a question and needs human input."""
    await web_push.dispatch_notification(
        "question", task_id,
        title=f"❓ Question: {task_id}",
        body=question[:200],
    )

    if not is_enabled():
        return

    thread_ts = _task_threads.get(task_id)
    text = f"\u2753 Question on {task_id}: {question[:500]}"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"\u2753 *CC has a question:*\n>{question[:500]}",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "_Reply on Switchboard to unblock the task_"},
            ],
        },
    ]

    await _post(text, thread_ts=thread_ts, blocks=blocks)


async def task_completed(task_id: str, turns: int, duration_s: float,
                         cost_usd: float, checklist_done: int, checklist_total: int,
                         result_preview: str | None = None):
    """Notify when a task completes successfully."""
    mins = int(duration_s // 60)
    secs = int(duration_s % 60)
    await web_push.dispatch_notification(
        "completed", task_id,
        title=f"✓ {task_id} completed",
        body=f"{checklist_done}/{checklist_total} · {mins}m{secs:02d}s · ${cost_usd:.2f}",
    )

    if not is_enabled():
        return

    thread_ts = _task_threads.get(task_id)
    bar = _progress_bar(checklist_done, checklist_total)
    progress = f"{checklist_done}/{checklist_total}"

    text = f"\u2705 Completed: {task_id} \u2014 {progress} | {turns} turns | {duration_s:.0f}s | ${cost_usd:.2f}"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"\u2705 *Completed:* `{task_id}`",
            },
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"`[{bar}]` *{progress}*"},
                {"type": "mrkdwn", "text": f"*Turns:* {turns}"},
                {"type": "mrkdwn", "text": f"*Duration:* {duration_s:.0f}s"},
                {"type": "mrkdwn", "text": f"*Cost:* ${cost_usd:.2f}"},
            ],
        },
    ]

    # Post result preview as a separate block if it's long
    if result_preview and len(result_preview) > 300:
        blocks.append({"type": "divider"})
        preview = result_preview[:2900] + "\n...(truncated)" if len(result_preview) > 2900 else result_preview
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": preview},
        })
    elif result_preview:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f">{result_preview[:500]}"},
        })

    await _post(text, thread_ts=thread_ts, blocks=blocks)


async def task_failed(task_id: str, error: str, turns: int = 0, cost_usd: float = 0):
    """Notify when a task fails."""
    await web_push.dispatch_notification(
        "failed", task_id,
        title=f"✕ {task_id} failed",
        body=error[:200],
    )

    if not is_enabled():
        return

    thread_ts = _task_threads.get(task_id)
    text = f"\u274c Failed: {task_id} \u2014 {error[:200]}"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"\u274c *Failed:* `{task_id}`\n```{error[:500]}```",
            },
        },
    ]
    if turns or cost_usd:
        blocks.append({
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"*Turns:* {turns}"},
                {"type": "mrkdwn", "text": f"*Cost:* ${cost_usd:.2f}"},
            ],
        })

    await _post(text, thread_ts=thread_ts, blocks=blocks)


async def task_attempt_starting(task_id: str, attempt: int, goal: str):
    """Notify when a reopened task starts a new attempt."""
    await web_push.dispatch_notification(
        "attempt_starting", task_id,
        title=f"Attempt {attempt} starting",
        body=goal[:200],
    )


async def task_needs_review(task_id: str, reason: str):
    """Notify when a task needs human review (timeout, no result, etc.)."""
    await web_push.dispatch_notification(
        "needs_review", task_id,
        title=f"⚠ {task_id} needs review",
        body=reason[:200],
    )

    if not is_enabled():
        return

    thread_ts = _task_threads.get(task_id)
    text = f"\u26a0\ufe0f Needs review: {task_id} \u2014 {reason[:200]}"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"\u26a0\ufe0f *Needs review:* `{task_id}`\n>{reason[:300]}",
            },
        },
    ]

    await _post(text, thread_ts=thread_ts, blocks=blocks)
