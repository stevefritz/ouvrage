#!/bin/bash
# PreToolUse hook for Bash. Only blocks `git push` to a remote.
# Reads the tool input JSON from stdin and inspects the command.

input=$(cat)
# Extract "command" value from {"tool_input":{"command":"..."},...}
command=$(echo "$input" | sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')

# Match `git push` at the start of the command, or after `&&`/`;`/`|`
if echo "$command" | grep -qE '(^|[;&|]\s*)git[[:space:]]+push([[:space:]]|$)'; then
    cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Direct git push is not available. Use mcp__switchboard__git_push(task_id=YOUR_TASK_ID) instead — the platform handles authentication."}}
EOF
fi

exit 0
