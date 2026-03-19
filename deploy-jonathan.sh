#!/bin/bash
# Deploy switchboard code to Jonathan's instance
set -e

SRC="/root/mcp-switchboard-test"
DEST="/opt/switchboard-jonathan"

echo "Deploying to switchboard-jonathan..."

# Copy Python files (skip test files)
for f in auth.py dashboard_api.py database.py notifications.py server.py tasks.py web_push.py; do
    cp "$SRC/$f" "$DEST/$f"
done

# Copy dashboard
cp -r "$SRC/dashboard/"* "$DEST/dashboard/"

# Fix ownership
chown -R switchboard-jonathan-svc:switchboard-jonathan "$DEST"

echo "Files copied. Restarting..."
systemctl restart switchboard-jonathan
sleep 2

if systemctl is-active --quiet switchboard-jonathan; then
    echo "switchboard-jonathan is running"
else
    echo "FAILED — check: journalctl -u switchboard-jonathan -n 30"
    exit 1
fi
