#!/bin/bash
cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Direct git fetch from remote is not available. Use mcp__switchboard__git_fetch(task_id=YOUR_TASK_ID) instead — the platform handles authentication. Local operations like git merge, git log, git diff work normally."}}
EOF
