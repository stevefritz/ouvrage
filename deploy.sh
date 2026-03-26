#!/bin/bash
# Deploy Switchboard code to one or more instances
# Usage:
#   ./deploy.sh                  # deploy to all instances
#   ./deploy.sh prod             # deploy to prod only
#   ./deploy.sh jonathan         # deploy to jonathan only
#   ./deploy.sh prod jonathan    # deploy to specific instances
set -e

SRC="$(cd "$(dirname "$0")" && pwd)"
# Instance registry: name -> (app_dir, service_name, owner_user, owner_group)
declare -A INSTANCES
INSTANCES[prod]="/opt/switchboard|switchboard|switchboard-svc|switchboard"
INSTANCES[test]="/opt/switchboard-test|switchboard-test|switchboard-test-svc|switchboard-test"
INSTANCES[jonathan]="/opt/switchboard-jonathan|switchboard-jonathan|switchboard-jonathan-svc|switchboard-jonathan"

deploy_instance() {
    local name="$1"
    local config="${INSTANCES[$name]}"
    if [ -z "$config" ]; then
        echo "Unknown instance: $name"
        echo "Available: ${!INSTANCES[*]}"
        return 1
    fi

    IFS='|' read -r app_dir service owner_user owner_group <<< "$config"

    echo "=== Deploying to $name ($app_dir) ==="

    # Copy switchboard package
    rsync -a --delete "$SRC/switchboard/" "$app_dir/switchboard/"

    # Copy dashboard
    if [ -d "$SRC/dashboard" ]; then
        cp -r "$SRC/dashboard/"* "$app_dir/dashboard/"
    fi

    # Fix ownership
    chown -R "$owner_user:$owner_group" "$app_dir"

    # Restart
    systemctl restart "$service"
    sleep 2

    if systemctl is-active --quiet "$service"; then
        echo "  ✓ $name is running"
    else
        echo "  ✗ $name FAILED — check: journalctl -u $service -n 30"
        return 1
    fi
}

# Determine targets
targets=("$@")
if [ ${#targets[@]} -eq 0 ]; then
    targets=("${!INSTANCES[@]}")
fi

echo "Deploying from: $SRC"
echo "Targets: ${targets[*]}"
echo ""

failed=0
for target in "${targets[@]}"; do
    deploy_instance "$target" || ((failed++))
    echo ""
done

if [ $failed -gt 0 ]; then
    echo "⚠ $failed instance(s) failed"
    exit 1
else
    echo "All instances deployed successfully"
fi
