"""ouvrage.dispatch._state — shared mutable state for dispatch submodules.

Extracted from engine.py to break circular imports. All dispatch submodules
can safely import from this module without triggering import cycles.
"""

import asyncio

from claude_agent_sdk import ClaudeSDKClient

# Track running async tasks to prevent garbage collection and silent failures
_running_tasks: set[asyncio.Task] = set()

# Track active SDK clients for tasks (used by cancel to interrupt)
_active_clients: dict[str, ClaudeSDKClient] = {}

# Track tasks with active gate coroutines in this process.
# Empty on startup — all gates are orphaned by definition (server restart killed them).
_running_gates: set[str] = set()

# Track gate asyncio.Task objects by task_id for cancellation (stop action).
_gate_tasks: dict[str, asyncio.Task] = {}
