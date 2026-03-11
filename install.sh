#!/usr/bin/env bash
set -euo pipefail

# Switchboard bare-metal install script
# Run as root on the VPS

APP_DIR="/opt/switchboard"
DATA_DIR="${APP_DIR}/data"
WORK_DIR="/work"
WORKER_USER="switchboard"

echo "=== Switchboard Install ==="

# Must run as root
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Must run as root"
    exit 1
fi

# Prerequisites check
command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }
command -v git >/dev/null || { echo "ERROR: git not found"; exit 1; }
command -v claude >/dev/null && echo "claude CLI: $(which claude)" || echo "WARNING: claude CLI not found — task dispatch won't work"

# Create restricted worker user for CC subprocesses
if ! id "$WORKER_USER" &>/dev/null; then
    echo "Creating restricted user: $WORKER_USER"
    useradd --system --shell /usr/sbin/nologin --home-dir "/home/$WORKER_USER" --create-home "$WORKER_USER"
    echo "$WORKER_USER created (no sudo, no docker, no login shell)"
else
    echo "User $WORKER_USER already exists"
fi

# Create directories
mkdir -p "$APP_DIR" "$DATA_DIR" "$WORK_DIR"
# Switchboard app owned by root (runs as root)
chown -R root:root "$APP_DIR"
# Work dir owned by worker user (CC writes here)
chown -R "$WORKER_USER:$WORKER_USER" "$WORK_DIR"
# Worker home dir for claude session files
chown -R "$WORKER_USER:$WORKER_USER" "/home/$WORKER_USER"

# Copy application files
echo "Copying application files to ${APP_DIR}..."
cp *.py pyproject.toml "$APP_DIR/"
cp -r dashboard/ "$APP_DIR/dashboard/"

# Install Python dependencies
echo "Installing Python dependencies..."
apt-get install -y python3-pip >/dev/null 2>&1 || true
pip3 install --quiet --break-system-packages "$APP_DIR" 2>/dev/null || \
    python3 -m pip install --break-system-packages "$APP_DIR"

# Migrate existing data if Docker container was running
DOCKER_NAME="infrastructure-switchboard-1"
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "$DOCKER_NAME"; then
    if [ ! -f "$DATA_DIR/switchboard.db" ]; then
        echo "Found existing Docker container. Copying database..."
        docker cp "$DOCKER_NAME:/data/switchboard.db" "$DATA_DIR/switchboard.db" 2>/dev/null || true
    else
        echo "Database already exists at $DATA_DIR/switchboard.db, skipping Docker migration"
    fi
fi

# Also check the local repo data dir
if [ ! -f "$DATA_DIR/switchboard.db" ] && [ -f "/root/mcp-switchboard/data/switchboard.db" ]; then
    echo "Copying database from repo data dir..."
    cp /root/mcp-switchboard/data/switchboard.db "$DATA_DIR/switchboard.db"
fi

# Install systemd service
echo "Installing systemd service..."
cp switchboard.service /etc/systemd/system/switchboard.service
systemctl daemon-reload
systemctl enable switchboard

echo ""
echo "=== Done ==="
echo "User:    $WORKER_USER (restricted — CC subprocesses run as this user)"
echo "Service: switchboard.service (runs as root)"
echo ""
echo "Start:   systemctl start switchboard"
echo "Status:  systemctl status switchboard"
echo "Logs:    journalctl -u switchboard -f"
echo "Health:  curl http://localhost:8100/health"
echo "Data:    ${DATA_DIR}/switchboard.db"
echo "Work:    ${WORK_DIR}/ (task worktrees, owned by $WORKER_USER)"
