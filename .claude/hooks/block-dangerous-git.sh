#!/bin/bash
# Block dangerous git commands in CC worker sessions
CMD=$(jq -r '.tool_input.command' < /dev/stdin)

# Force push (all variants)
if echo "$CMD" | grep -qE 'git push.*(--force|--force-with-lease|\s-f\s|-f$)'; then
  echo "Force push is blocked. To resolve conflicts, use: git merge origin/main. If push still fails, push to a rescue branch: git push origin HEAD:{branch}-rescue-1 and post a question." >&2
  exit 2
fi

# Rebase
if echo "$CMD" | grep -qE 'git rebase'; then
  echo "Rebase is blocked. Use 'git merge origin/main' instead. Never rebase in Ouvrage." >&2
  exit 2
fi

# Remote manipulation
if echo "$CMD" | grep -qE 'git remote (add|set-url|remove|rename)'; then
  echo "Modifying git remotes is blocked. You work with origin only." >&2
  exit 2
fi

# Tag creation
if echo "$CMD" | grep -qE 'git tag|git push.*--tags'; then
  echo "Creating git tags is blocked. Tags are managed by the deployment pipeline." >&2
  exit 2
fi

exit 0
