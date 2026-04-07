#!/bin/bash
cat <<'EOF'
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Direct git push is not available. Use mcp__switchboard__git_push(task_id=YOUR_TASK_ID) instead — the platform handles authentication."}}
EOF
