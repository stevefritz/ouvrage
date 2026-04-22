#!/bin/bash
# Dev-image entrypoint — master key resolution with persistence.
#
# The previous Dockerfile.dev CMD generated a fresh Fernet key on every
# container start if no env var was set. /data is a named volume that
# persists (encrypted PATs, OAuth secrets, etc.), but the ephemeral key
# did not — every restart made prior encrypted data permanently
# unreadable. This script persists a generated key to /data/.master_key
# on first boot and re-uses it on subsequent boots.
#
# Resolution order:
#   1. $OUVRAGE_MASTER_KEY env var (explicit; wins)
#   2. /data/.master_key file from a prior run (re-used)
#   3. Generate a new key, persist to /data/.master_key, warn loudly

set -euo pipefail

mkdir -p /data
KEY_FILE="/data/.master_key"

if [ -n "${OUVRAGE_MASTER_KEY:-}" ]; then
    : # explicit env var wins
elif [ -f "$KEY_FILE" ]; then
    OUVRAGE_MASTER_KEY="$(cat "$KEY_FILE")"
    export OUVRAGE_MASTER_KEY
    echo "[dev] Reusing persisted master key from $KEY_FILE" >&2
else
    OUVRAGE_MASTER_KEY="$(python3 -m ouvrage generate-key)"
    export OUVRAGE_MASTER_KEY
    umask 077
    printf '%s\n' "$OUVRAGE_MASTER_KEY" > "$KEY_FILE"
    echo "[dev] Generated NEW master key and persisted to $KEY_FILE" >&2
    echo "[dev] WARNING: treat this as ephemeral — for a stable key set OUVRAGE_MASTER_KEY explicitly." >&2
fi

exec python3 -m ouvrage
