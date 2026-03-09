#!/usr/bin/env bash
set -euo pipefail

# Switchboard bare-metal install script
# Run as root or with sudo on the VPS

APP_DIR="/opt/switchboard"
DATA_DIR="${APP_DIR}/data"
WORK_DIR="/work"
SERVICE_USER="ubuntu"

echo "=== Switchboard Install ==="

# Prerequisites check
command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }
command -v git >/dev/null || { echo "ERROR: git not found"; exit 1; }
command -v claude >/dev/null && echo "claude CLI: $(which claude)" || echo "WARNING: claude CLI not found — task dispatch won't work"

# Create directories
mkdir -p "$APP_DIR" "$DATA_DIR" "$WORK_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR" "$WORK_DIR"

# Copy application files
echo "Copying application files to ${APP_DIR}..."
cp server.py database.py tasks.py auth.py pyproject.toml "$APP_DIR/"

# Install Python dependencies
echo "Installing Python dependencies..."
pip3 install --quiet aiosqlite 'mcp[cli]>=1.2.0' 'uvicorn>=0.34.0' 'httpx>=0.28.0' 'pyjwt[crypto]>=2.11.0' 'claude-agent-sdk>=0.1.0'

# Migrate existing data if Docker container was running
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q switchboard; then
    echo "Found existing Docker container. Copying database..."
    docker cp switchboard:/data/switchboard.db "$DATA_DIR/switchboard.db" 2>/dev/null || true
    echo "Stopping Docker container..."
    docker stop switchboard 2>/dev/null || true
    docker rm switchboard 2>/dev/null || true
fi

# If data dir has no DB yet, it'll be created on first run
chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"

# Install systemd service
echo "Installing systemd service..."
cp switchboard.service /etc/systemd/system/switchboard.service
systemctl daemon-reload
systemctl enable switchboard
systemctl start switchboard

echo ""
echo "=== Done ==="
echo "Status:  systemctl status switchboard"
echo "Logs:    journalctl -u switchboard -f"
echo "Health:  curl http://localhost:8100/health"
echo "Data:    ${DATA_DIR}/switchboard.db"
echo "Work:    ${WORK_DIR}/ (task worktrees)"
