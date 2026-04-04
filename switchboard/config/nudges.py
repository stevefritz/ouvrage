"""Behavioral nudge system — category-weighted reminders appended to MCP tool responses.

Nudges keep behavioral rules fresh in context as sessions grow long.
Each tool response gets one nudge, selected from a weighted random pool.
The tool just called boosts its associated category (3x weight).
"""

import random

NUDGE_CATEGORIES: dict[str, list[str]] = {
    "planning": [
        "Propose your plan and get approval before dispatching tasks.",
        "Read project context (get_context, get_pinned) before starting work.",
        "Present options with tradeoffs — don't just pick one approach.",
        "Suggest a discovery chain if the project lacks documentation.",
    ],
    "dispatch": [
        "Confirm with the user before dispatching tasks.",
        "Chain tasks that modify the same files. Parallel for independent work.",
        "Don't set both auto_merge and auto_pr on the same task.",
        "Include a checklist of acceptance criteria on every dispatch.",
    ],
    "communication": [
        "Post important decisions to conversations so future sessions can find them.",
        "Update the pinned status message after major work sessions.",
        "Author should be 'claude-ai', not the user's name.",
        "Use table views for status summaries — the user might be on mobile.",
    ],
    "search": [
        "Search returns pointers, not full content. Use read(around=id) for detail.",
        "Check search before asking the user — the answer might already exist.",
        "Read pinned messages on key conversations before starting work.",
    ],
    "quality": [
        "Sonnet implements, Opus reviews. Don't waste Opus on implementation.",
        "Don't write implementation plans in specs — CC does that during grounding.",
        "When something fails, read the session log before retrying.",
        "Keep specs bounded: situation, what to do, reference.",
    ],
    "interaction": [
        "When the user says 'go' or 'do it', that's approval to dispatch.",
        "When asked 'what do you think', give your actual opinion.",
        "Diagnose failures before retrying — read get_session_log first.",
        "Call get_guide if you need a refresher on workflows.",
    ],
}

# Maps tool names to their most relevant nudge category.
# Tools not in this map get all categories at baseline weight.
TOOL_CATEGORY_MAP: dict[str, str | None] = {
    "dispatch_task": "dispatch",
    "transition_task": "dispatch",
    "get_task_status": "quality",
    "list_tasks": "planning",
    "search": "search",
    "read": "search",
    "post": "communication",
    "post_task_message": "communication",
    "get_pinned": "search",
    "conversations": "planning",
    "create_conversation": "communication",
    "get_context": "planning",
    "get_guide": None,  # No nudge — the guide IS the behavioral reference
    "get_session_log": "quality",
    "get_dispatch_log": "quality",
}

# Tools that never receive a nudge
_NO_NUDGE_TOOLS = frozenset({"get_guide", "get_context"})


def select_nudge(tool_name: str) -> str | None:
    """Select a nudge for the given tool, or None if the tool should not be nudged.

    The tool's associated category gets 3x weight; all other categories get 1x.
    """
    if tool_name in _NO_NUDGE_TOOLS:
        return None

    boosted_category = TOOL_CATEGORY_MAP.get(tool_name)

    # Build weighted pool: list of (nudge_text, weight)
    pool: list[tuple[str, int]] = []
    for category, nudges in NUDGE_CATEGORIES.items():
        weight = 3 if category == boosted_category else 1
        for nudge in nudges:
            pool.append((nudge, weight))

    if not pool:
        return None

    total = sum(w for _, w in pool)
    r = random.random() * total
    cumulative = 0.0
    for nudge, weight in pool:
        cumulative += weight
        if r <= cumulative:
            return nudge
    return pool[-1][0]


def inject_nudge(result: dict, tool_name: str) -> None:
    """Inject a behavioral nudge into a tool response dict as a _nudge field."""
    nudge = select_nudge(tool_name)
    if nudge:
        result["_nudge"] = f"\U0001f4a1 {nudge}"
