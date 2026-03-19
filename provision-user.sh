#!/bin/bash
# Provision a new Switchboard user instance
# Usage: ./provision-user.sh <username> <password> <email> [port]
#
# This script:
#   1. Creates system users (service + worker)
#   2. Creates app dir, copies code, creates DB
#   3. Creates worktree dir
#   4. Sets up worker home (CC settings, gitconfig, SSH key)
#   5. Creates systemd service
#   6. Adds UFW rule
#   7. Adds Authelia user
#   8. Adds Caddy route (forward auth)
#   9. Prints manual steps (DNS, GitHub SSH key, CC auth)
set -e

if [ $# -lt 3 ]; then
    echo "Usage: $0 <username> <password> <email> [port]"
    echo "Example: $0 jonathan 'MyPassword123' jonathan@example.com"
    exit 1
fi

USERNAME="$1"
PASSWORD="$2"
EMAIL="$3"
PORT="${4:-$(shuf -i 8110-8199 -n 1)}"  # random port if not specified

SRC="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="/opt/switchboard-${USERNAME}"
WORK_DIR="/work-${USERNAME}"
SERVICE="switchboard-${USERNAME}"
SVC_USER="switchboard-${USERNAME}-svc"
WORKER_USER="switchboard-${USERNAME}"
GROUP="switchboard-${USERNAME}"
SUBDOMAIN="switchboard-${USERNAME}.stephenfritz.dev"
VPS_IP="51.222.159.155"

echo "=== Provisioning Switchboard for: ${USERNAME} ==="
echo "  App dir:   ${APP_DIR}"
echo "  Work dir:  ${WORK_DIR}"
echo "  Service:   ${SERVICE}"
echo "  Port:      ${PORT}"
echo "  Subdomain: ${SUBDOMAIN}"
echo ""

# ── 1. System users ──────────────────────────────────────────
echo "[1/9] Creating system users..."
groupadd "${GROUP}" 2>/dev/null || true
useradd -r -g "${GROUP}" -s /usr/sbin/nologin "${SVC_USER}" 2>/dev/null || echo "  ${SVC_USER} exists"
useradd -r -g "${GROUP}" -s /bin/bash -m "${WORKER_USER}" 2>/dev/null || echo "  ${WORKER_USER} exists"

# ── 2. App directory ─────────────────────────────────────────
echo "[2/9] Setting up app directory..."
mkdir -p "${APP_DIR}/data"
for f in auth.py dashboard_api.py database.py notifications.py server.py tasks.py web_push.py; do
    [ -f "$SRC/$f" ] && cp "$SRC/$f" "${APP_DIR}/$f"
done
[ -d "$SRC/dashboard" ] && cp -r "$SRC/dashboard" "${APP_DIR}/"
chown -R "${SVC_USER}:${GROUP}" "${APP_DIR}"

# ── 3. Worktree directory ────────────────────────────────────
echo "[3/9] Creating worktree directory..."
mkdir -p "${WORK_DIR}"
chown "${WORKER_USER}:${GROUP}" "${WORK_DIR}"

# ── 4. Worker home setup ─────────────────────────────────────
echo "[4/9] Setting up worker home..."
WORKER_HOME="/home/${WORKER_USER}"
mkdir -p "${WORKER_HOME}/.claude" "${WORKER_HOME}/.ssh"

# CC settings
cat > "${WORKER_HOME}/.claude/settings.json" << 'SETTINGS'
{
  "attribution": {
    "commit": "",
    "pr": ""
  },
  "includeCoAuthoredBy": false,
  "permissions": {
    "defaultMode": "default"
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "bash -c 'input=$(cat); cmd=$(echo \"$input\" | jq -r .tool_input.command // empty); if echo \"$cmd\" | grep -qE \"\\bpkill\\b|\\bkillall\\b|\\bkill\\s|\\bkill\\s+-\"; then echo \"BLOCKED: Do not use pkill/killall/kill — you will terminate yourself. Use task timeouts instead.\" >&2; exit 2; fi'"
          }
        ]
      }
    ]
  }
}
SETTINGS

# Git config
cat > "${WORKER_HOME}/.gitconfig" << 'GITCONFIG'
[safe]
	directory = *
[user]
	name = Switchboard Automation
	email = switchboard@stephenfritz.dev
GITCONFIG

# SSH key for git operations
if [ ! -f "${WORKER_HOME}/.ssh/id_ed25519" ]; then
    ssh-keygen -t ed25519 -f "${WORKER_HOME}/.ssh/id_ed25519" -N "" -C "${SERVICE}@vps" > /dev/null
    echo "  Generated SSH key"
fi

# Known hosts for GitHub — both worker user AND login user
ssh-keyscan github.com >> "${WORKER_HOME}/.ssh/known_hosts" 2>/dev/null
sort -u "${WORKER_HOME}/.ssh/known_hosts" -o "${WORKER_HOME}/.ssh/known_hosts"

chown -R "${WORKER_USER}:${GROUP}" "${WORKER_HOME}"

# Also set up the login user's SSH (they SSH in as $USERNAME, not the worker)
LOGIN_HOME="/home/${USERNAME}"
if [ -d "${LOGIN_HOME}" ] && [ "${LOGIN_HOME}" != "${WORKER_HOME}" ]; then
    mkdir -p "${LOGIN_HOME}/.ssh"
    ssh-keyscan github.com >> "${LOGIN_HOME}/.ssh/known_hosts" 2>/dev/null
    sort -u "${LOGIN_HOME}/.ssh/known_hosts" -o "${LOGIN_HOME}/.ssh/known_hosts"
    chown -R "${USERNAME}:${USERNAME}" "${LOGIN_HOME}/.ssh"
    chmod 700 "${LOGIN_HOME}/.ssh"
fi
chmod 700 "${WORKER_HOME}/.ssh"
chmod 600 "${WORKER_HOME}/.ssh/id_ed25519" "${WORKER_HOME}/.ssh/authorized_keys" 2>/dev/null || true

# ── 5. Systemd service ───────────────────────────────────────
echo "[5/9] Creating systemd service..."
cat > "/etc/systemd/system/${SERVICE}.service" << EOF
[Unit]
Description=Switchboard ${USERNAME} MCP Server
After=network.target

[Service]
User=${SVC_USER}
Group=${GROUP}
WorkingDirectory=${APP_DIR}
ExecStart=/usr/bin/python3 server.py
Environment=SWITCHBOARD_DB=${APP_DIR}/data/switchboard.db
Environment=SWITCHBOARD_PORT=${PORT}
Environment=AUTH_ISSUER_URL=https://auth.stephenfritz.dev
Environment=RESOURCE_URL=https://${SUBDOMAIN}/mcp
Environment=CLAUDE_CODE_STREAM_CLOSE_TIMEOUT=120000
Environment=WORKER_USER=${WORKER_USER}
Environment=WORKTREE_BASE=${WORK_DIR}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

ProtectSystem=strict
PrivateTmp=true
NoNewPrivileges=false
ReadWritePaths=${APP_DIR} ${WORK_DIR} ${WORKER_HOME} /tmp
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=false

AmbientCapabilities=CAP_SETUID CAP_SETGID CAP_KILL
CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_KILL CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE}" --quiet

# ── 6. UFW rule ──────────────────────────────────────────────
echo "[6/9] Adding UFW rule..."
ufw allow from 172.16.0.0/12 to any port "${PORT}" comment "${SERVICE}" 2>/dev/null || true

# ── 7. Authelia user ─────────────────────────────────────────
echo "[7/9] Adding Authelia user..."
AUTHELIA_USERS="/root/infrastructure/authelia/users.yml"
if grep -q "^  ${USERNAME}:" "${AUTHELIA_USERS}" 2>/dev/null; then
    echo "  User ${USERNAME} already exists in Authelia"
else
    HASH=$(docker run --rm authelia/authelia:latest authelia crypto hash generate argon2 --password "${PASSWORD}" 2>/dev/null | grep 'Digest:' | awk '{print $2}')
    cat >> "${AUTHELIA_USERS}" << EOF
  ${USERNAME}:
    disabled: false
    displayname: ${USERNAME}
    email: ${EMAIL}
    password: '${HASH}'
    groups:
      - users
EOF
    echo "  Added ${USERNAME} to Authelia"
    (cd /root/infrastructure && docker compose up -d --force-recreate authelia 2>&1 | tail -1)
fi

# ── 8. Caddy route ───────────────────────────────────────────
echo "[8/9] Adding Caddy route..."
CADDYFILE="/root/infrastructure/Caddyfile"
if grep -q "${SUBDOMAIN}" "${CADDYFILE}" 2>/dev/null; then
    echo "  Route already exists in Caddyfile"
else
    # Insert before the last closing brace or at end
    cat >> "${CADDYFILE}" << EOF

${SUBDOMAIN} {
	import security_headers
	@dashboard path /dashboard*
	handle @dashboard {
		forward_auth authelia:9091 {
			uri /api/authz/forward-auth
			copy_headers Remote-User Remote-Groups
		}
		reverse_proxy host.docker.internal:${PORT} {
			flush_interval -1
		}
	}
	handle {
		reverse_proxy host.docker.internal:${PORT} {
			flush_interval -1
		}
	}
}
EOF
    echo "  Added Caddy route"
    (cd /root/infrastructure && docker compose up -d --force-recreate caddy 2>&1 | tail -1)
fi

# ── 9. Start service ─────────────────────────────────────────
echo "[9/9] Starting service..."
systemctl start "${SERVICE}"
sleep 3
if systemctl is-active --quiet "${SERVICE}"; then
    echo "  ✓ ${SERVICE} is running on port ${PORT}"
else
    echo "  ✗ FAILED — check: journalctl -u ${SERVICE} -n 30"
fi

# ── Summary ──────────────────────────────────────────────────
SSH_PUBKEY=$(cat "${WORKER_HOME}/.ssh/id_ed25519.pub")

echo ""
echo "════════════════════════════════════════════════════════"
echo " PROVISIONING COMPLETE"
echo "════════════════════════════════════════════════════════"
echo ""
echo "MANUAL STEPS REMAINING:"
echo ""
echo "1. ADD CLOUDFLARE DNS RECORD:"
echo "   Type: A"
echo "   Name: switchboard-${USERNAME}"
echo "   Content: ${VPS_IP}"
echo ""
echo "2. ADD THIS SSH KEY TO GITHUB (for git clone/push):"
echo "   ${SSH_PUBKEY}"
echo "   → https://github.com/settings/keys"
echo "   Title: ${SERVICE}@vps"
echo ""
echo "3. USER MUST SSH IN AND AUTHORIZE CLAUDE CODE:"
echo "   ssh ${USERNAME}@${VPS_IP}"
echo "   Then run: claude auth"
echo ""
echo "════════════════════════════════════════════════════════"
echo ""
echo "TEXT TO SEND THE USER:"
echo "────────────────────────────────────────────────────────"
cat << USERTEXT

Switchboard Access — ${USERNAME}
=============================

SSH:
ssh ${USERNAME}@${VPS_IP}

Dashboard:
https://${SUBDOMAIN}/dashboard
Log in through Authelia:
  Username: ${USERNAME}
  Password: ${PASSWORD}

Claude.ai MCP Connector:
  Server URL: https://${SUBDOMAIN}/mcp
  Client ID: claude-mcp
  Client Secret: cd1e49318a854b874862bf73c7e35d3d
  Auth URL: https://auth.stephenfritz.dev/api/oidc/authorization
  Token URL: https://auth.stephenfritz.dev/api/oidc/token
  Scopes: openid profile email offline_access

When Claude.ai asks you to authenticate:
  Username: ${USERNAME}
  Password: ${PASSWORD}

Setup:
1. SSH in to the server
2. Authorize Claude Code: run 'claude auth'
3. Generate an SSH key and add it to your GitHub:
     ssh-keygen -t ed25519
     cat ~/.ssh/id_ed25519.pub
   Copy that key → https://github.com/settings/keys
4. Any project needs a pre-existing git repo with at least one
   commit pushed (worktree limitation — empty repos won't work)
5. Use Claude.ai with Switchboard tools to create projects and
   dispatch tasks. Watch progress on the dashboard.

USERTEXT
echo "────────────────────────────────────────────────────────"
