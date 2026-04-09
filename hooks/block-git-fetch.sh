#!/bin/bash
# PreToolUse hook for Bash. Only blocks `git fetch` from a remote.
# Reads the tool input JSON from stdin and inspects the command.

input=$(cat)
# Extract "command" value from {"tool_input":{"command":"..."},...}
command=$(echo "$input" | sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')

# Match `git fetch` at the start of the command, or after `&&`/`;`/`|`
if echo "$command" | grep -qE '(^|[;&|]\s*)git[[:space:]]+fetch([[:space:]]|$)'; then
    cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Direct git fetch from remote is not available. Use mcp__switchboard__git_fetch(task_id=YOUR_TASK_ID) instead — the platform handles authentication. Local operations like git merge, git log, git diff work normally."}}
EOF
fi

exit 0
