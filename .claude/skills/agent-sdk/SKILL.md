---
name: agent-sdk
description: "Build apps with the Claude Agent SDK (claude_agent_sdk). TRIGGER when: code imports `claude_agent_sdk`, or user asks to use the Agent SDK, run CC workers programmatically, manage sessions, or resume conversations. DO NOT TRIGGER when: code imports `anthropic` (direct API), general Python, or ML tasks."
license: Content sourced from https://github.com/anthropics/skills (skills/claude-api/python/agent-sdk/)
---

# Claude Agent SDK — Python Reference

The Claude Agent SDK provides a higher-level interface for running Claude Code agents programmatically, with built-in tools, session management, and MCP support.

## Installation

```bash
pip install claude-agent-sdk
```

---

## Quick Start

```python
import anyio
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

async def main():
    async for message in query(
        prompt="Explain this codebase",
        options=ClaudeAgentOptions(allowed_tools=["Read", "Glob", "Grep"])
    ):
        if isinstance(message, ResultMessage):
            print(message.result)

anyio.run(main)
```

---

## Primary Interfaces

### `query()` — Simple One-Shot Usage

```python
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

async for message in query(
    prompt="Explain this codebase",
    options=ClaudeAgentOptions(
        cwd="/path/to/project",
        allowed_tools=["Read", "Glob", "Grep"]
    )
):
    if isinstance(message, ResultMessage):
        print(message.result)
```

### `ClaudeSDKClient` — Full Control (Multi-Turn Conversations)

Use `ClaudeSDKClient` when you need multi-turn conversations, custom tools, hooks, streaming, or the ability to interrupt execution.

```python
import anyio
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, AssistantMessage, TextBlock

async def main():
    options = ClaudeAgentOptions(allowed_tools=["Read", "Glob", "Grep"])
    async with ClaudeSDKClient(options=options) as client:
        await client.query("Explain this codebase")
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text)

anyio.run(main)
```

`ClaudeSDKClient` supports:
- **Context manager** (`async with`) for automatic resource cleanup
- **`client.query(prompt)`** to send a prompt to the agent
- **`receive_response()`** for streaming messages until completion
- **`interrupt()`** to stop agent execution mid-task
- **Required for custom tools** (via SDK MCP servers)

---

## Message Types

```python
from claude_agent_sdk import (
    query, ClaudeAgentOptions,
    SystemMessage,       # System events (init, etc.)
    AssistantMessage,    # Agent text/tool output
    ResultMessage,       # Final result when agent finishes
    # StreamEvent,       # Raw stream events (advanced use)
)

async for message in query(prompt="...", options=ClaudeAgentOptions()):
    if isinstance(message, SystemMessage) and message.subtype == "init":
        session_id = message.data.get("session_id")  # Capture for session resume

    elif isinstance(message, AssistantMessage):
        # Per-turn content and usage data
        if message.usage:
            print(f"Tokens: {message.usage['input_tokens']} in, {message.usage['output_tokens']} out")

    elif isinstance(message, ResultMessage):
        print(message.result)
        print(f"Stop reason: {message.stop_reason}")  # "end_turn", "max_turns", etc.
        # ResultMessage also carries session_id on clean completion:
        session_id = getattr(message, 'session_id', None)
```

Additional typed message subclasses for subagent task events:
- `TaskStartedMessage` — emitted when a subagent task is registered
- `TaskProgressMessage` — real-time progress updates with cumulative usage metrics
- `TaskNotificationMessage` — task completion notifications
- `RateLimitEvent` — rate limit status transitions (`allowed`, `allowed_warning`, `rejected`)

---

## Session Resumption

### Capture Session ID from SystemMessage init

```python
import anyio
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage, SystemMessage

async def main():
    session_id = None

    # First query — capture the session ID from the init event
    async for message in query(
        prompt="Read the authentication module",
        options=ClaudeAgentOptions(allowed_tools=["Read", "Glob"])
    ):
        if isinstance(message, SystemMessage) and message.subtype == "init":
            session_id = message.data.get("session_id")

    # Resume with full context from the first query
    async for message in query(
        prompt="Now find all places that call it",  # "it" = auth module
        options=ClaudeAgentOptions(resume=session_id)
    ):
        if isinstance(message, ResultMessage):
            print(message.result)

anyio.run(main)
```

### Resume by Explicit Session ID (`ClaudeAgentOptions.resume`)

```python
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

async for message in query(
    prompt="Continue where you left off",
    options=ClaudeAgentOptions(resume="<captured-session-id>")
):
    if isinstance(message, ResultMessage):
        print(message.result)
```

### Resume Most Recent Session by cwd (`continue_conversation`)

```python
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

# continue_conversation=True resumes the most recent session for the given cwd
async for message in query(
    prompt="Continue the refactoring",
    options=ClaudeAgentOptions(
        cwd="/path/to/project",
        continue_conversation=True
    )
):
    if isinstance(message, ResultMessage):
        print(message.result)
```

---

## Session Discovery

```python
from claude_agent_sdk import list_sessions, get_session_messages, get_session_info

# List all past sessions (sync — no await)
sessions = list_sessions()
for session in sessions:
    print(f"Session {session.session_id} in {session.cwd}")

# Get info about a specific session (sync — no await)
info = get_session_info(session_id="<session-id>")
print(info)

# Retrieve messages from a session (sync — no await)
messages = get_session_messages(session_id="<session-id>")
for msg in messages:
    print(msg)
```

---

## Common Options (`ClaudeAgentOptions`)

| Option                | Type   | Description                                              |
| --------------------- | ------ | -------------------------------------------------------- |
| `cwd`                 | string | Working directory for file operations                    |
| `allowed_tools`       | list   | Tools the agent can use (e.g., `["Read", "Edit", "Bash"]`) |
| `permission_mode`     | string | `"default"`, `"plan"`, `"acceptEdits"`, `"bypassPermissions"` |
| `resume`              | string | Session ID to resume                                     |
| `continue_conversation` | bool | Resume most recent session for `cwd`                    |
| `max_turns`           | int    | Max agent turns before stopping                          |
| `max_budget_usd`      | float  | Max budget in USD                                        |
| `model`               | string | Model ID override                                        |
| `mcp_servers`         | dict   | MCP servers to connect to                                |
| `hooks`               | dict   | Pre/post tool hooks                                      |
| `system_prompt`       | string | Custom system prompt                                     |
| `agents`              | dict   | Subagent definitions (`dict[str, AgentDefinition]`)      |
| `setting_sources`     | list   | Settings to load (e.g., `["project"]` for CLAUDE.md)    |
| `env`                 | dict   | Environment variables for the session                    |

---

## Built-in Tools

| Tool            | Description                           |
| --------------- | ------------------------------------- |
| Read            | Read files in the workspace           |
| Write           | Create new files                      |
| Edit            | Make precise edits to existing files  |
| Bash            | Execute shell commands                |
| Glob            | Find files by pattern                 |
| Grep            | Search files by content               |
| WebSearch       | Search the web                        |
| WebFetch        | Fetch and analyze web pages           |
| AskUserQuestion | Ask user clarifying questions         |
| Agent           | Spawn subagents                       |

---

## Custom Tools (via SDK MCP Server)

Custom tools require `ClaudeSDKClient` (not `query()`):

```python
import anyio
from claude_agent_sdk import (
    tool, create_sdk_mcp_server,
    ClaudeSDKClient, ClaudeAgentOptions,
    AssistantMessage, TextBlock,
)

@tool("get_weather", "Get the current weather for a location", {"location": str})
async def get_weather(args):
    location = args["location"]
    return {"content": [{"type": "text", "text": f"Weather in {location}: sunny 72°F"}]}

server = create_sdk_mcp_server("weather-tools", tools=[get_weather])

async def main():
    options = ClaudeAgentOptions(mcp_servers={"weather": server})
    async with ClaudeSDKClient(options=options) as client:
        await client.query("What's the weather in Paris?")
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text)

anyio.run(main)
```

---

## Hooks

```python
from claude_agent_sdk import query, ClaudeAgentOptions, HookMatcher, ResultMessage

async def log_file_change(input_data, tool_use_id, context):
    file_path = input_data.get('tool_input', {}).get('file_path', 'unknown')
    print(f"Modified: {file_path}")
    return {}

async for message in query(
    prompt="Refactor utils.py",
    options=ClaudeAgentOptions(
        permission_mode="acceptEdits",
        hooks={
            "PostToolUse": [HookMatcher(matcher="Edit|Write", hooks=[log_file_change])]
        }
    )
):
    if isinstance(message, ResultMessage):
        print(message.result)
```

Available hook events: `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `PreCompact`, `Notification`, `SubagentStart`, `PermissionRequest`

---

## Error Handling

```python
from claude_agent_sdk import (
    query, ClaudeAgentOptions,
    CLINotFoundError, CLIConnectionError, ProcessError,
    ResultMessage,
)

try:
    async for message in query(
        prompt="Fix the failing tests",
        options=ClaudeAgentOptions(allowed_tools=["Read", "Edit", "Bash"], max_turns=10)
    ):
        if isinstance(message, ResultMessage):
            print(message.result)
except CLINotFoundError:
    print("Claude Code CLI not found. Install with: pip install claude-agent-sdk")
except CLIConnectionError as e:
    print(f"Connection error: {e}")
except ProcessError as e:
    print(f"Process error: {e}")
```

---

## Session Mutations

```python
from claude_agent_sdk import rename_session, tag_session

# Rename a session (sync — no await)
rename_session(session_id="...", title="Refactoring auth module")

# Tag a session (sync — no await)
tag_session(session_id="...", tag="experiment-v2")

# Clear a tag
tag_session(session_id="...", tag=None)
```

---

## MCP Server Management (ClaudeSDKClient)

```python
async with ClaudeSDKClient(options=options) as client:
    await client.reconnect_mcp_server("my-server")
    await client.toggle_mcp_server("my-server", enabled=False)
    status = await client.get_mcp_status()
```

---

## Best Practices

1. **Always specify `allowed_tools`** — Explicitly list which tools the agent can use
2. **Set `cwd`** — Always specify working directory for file operations
3. **Capture session ID early** — Listen for `SystemMessage(subtype="init")` and store `message.data.get("session_id")` immediately if you need to resume later
4. **Use `ResultMessage.session_id`** for clean-completion session ID (when available)
5. **Use `resume=session_id`** for explicit resume; `continue_conversation=True` for "pick up where I left off in this directory"
6. **Limit `max_turns`** — Prevent runaway agents
7. **Use `ClaudeSDKClient`** for multi-turn conversations and custom tools; use `query()` for simple one-shot tasks
