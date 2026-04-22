#!/bin/bash
set -euo pipefail

# --- Fix volume ownership ---
# Mounted volumes start as root, OR may carry over from a previous container
# that used a different UID (we changed worker UID from 999 → 1001 in the
# multi-runtime image). Recursively reconcile so the app and workers can read.
chown switchboard-svc:switchboard /data
chown switchboard:switchboard /work

# Worker home subdirs that get bind-mounted (./claude-auth → ~/.claude,
# ./gitconfig → ~/.gitconfig). If files inside are owned by a stale UID
# (e.g. 999 from the previous image), the worker can't read its own
# .credentials.json and CC dispatch silently fails with worker_has_oauth=False.
# Only chown if the dir exists and ownership is wrong (avoids needless work).
WORKER_UID=$(id -u switchboard)
WORKER_GID=$(id -g switchboard)
if [ -d /home/switchboard/.claude ]; then
    if [ "$(stat -c %u /home/switchboard/.claude)" != "$WORKER_UID" ]; then
        echo "[entrypoint] Reconciling /home/switchboard/.claude ownership to ${WORKER_UID}:${WORKER_GID}"
        chown -R "${WORKER_UID}:${WORKER_GID}" /home/switchboard/.claude
    fi
fi

# --- Temp directory ---
# TMPDIR=/work/.tmp redirects all app temp files (pytest, CC sessions) to the
# work volume instead of /tmp. tmpreaper runs hourly to clean files older than 2h.
mkdir -p /work/.tmp
chown switchboard:switchboard /work/.tmp
chmod 1777 /work/.tmp

# Clean any stale temp files from previous runs
tmpreaper 2h /work/.tmp 2>/dev/null || true

# Background tmpreaper loop — cleans /work/.tmp every hour
(while true; do sleep 3600; tmpreaper 2h /work/.tmp 2>/dev/null || true; done) &

# --- Uploads directory ---
# Lives in /work so CC workers can read uploaded files directly.
# Owned by service user (writes), group-readable by worker (reads).
mkdir -p /work/.uploads
chown switchboard-svc:switchboard /work/.uploads
chmod 770 /work/.uploads

# --- Master key resolution ---
# Priority: Docker secret file (secure) > env var (bare metal) > generate + warn
SECRET_FILE="/run/secrets/master_key"
if [ -f "$SECRET_FILE" ]; then
    # Python reads directly from file via crypto.get_master_key()
    # migrate-auth runs as root (before gosu) so it can still read the file
    # After this block, entrypoint chowns to service user so worker can't read it
    echo "[entrypoint] Master key found at Docker secret"
elif [ -z "${OUVRAGE_MASTER_KEY:-}" ]; then
    export OUVRAGE_MASTER_KEY
    OUVRAGE_MASTER_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    echo "[entrypoint] WARNING: No /run/secrets/master_key or OUVRAGE_MASTER_KEY env var found"
    echo "[entrypoint] Generated ephemeral key — credentials will be unrecoverable if container restarts"
    echo "[entrypoint] Set OUVRAGE_MASTER_KEY or use Docker secrets for production"
fi

# --- OAuth RSA key ---
# Auto-generated on first boot if missing. Persists in /data volume.
RSA_PATH="${OAUTH_RSA_KEY_PATH:-/data/oauth_rsa_key.pem}"
if [ ! -f "$RSA_PATH" ]; then
    echo "[entrypoint] Generating OAuth RSA key at $RSA_PATH"
    python3 -c "
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
with open('$RSA_PATH', 'wb') as f:
    f.write(pem)
"
    chown switchboard-svc:switchboard "$RSA_PATH"
fi

# --- DB migration ---
# migrate-auth is idempotent — safe to run every boot.
# Only runs if owner credentials are provided (SaaS mode: control plane calls
# POST /internal/bootstrap-user instead, so these vars won't be set).
# Accept either OUVRAGE_OWNER_PASSWORD (plaintext, hashed inside migrate-auth)
# or OUVRAGE_OWNER_PASSWORD_HASH (pre-hashed). The CLI handles either.
if [ -n "${OUVRAGE_OWNER_EMAIL:-}" ] && { [ -n "${OUVRAGE_OWNER_PASSWORD_HASH:-}" ] || [ -n "${OUVRAGE_OWNER_PASSWORD:-}" ]; }; then
    echo "[entrypoint] Running migrate-auth..."
    MIGRATE_ARGS=(
        --email "${OUVRAGE_OWNER_EMAIL}"
        --name "${OUVRAGE_OWNER_NAME:-Owner}"
        --slug "${OUVRAGE_INSTANCE_SLUG:-default}"
        --instance-name "${OUVRAGE_INSTANCE_NAME:-Ouvrage}"
    )
    if [ -n "${OUVRAGE_OWNER_PASSWORD_HASH:-}" ]; then
        MIGRATE_ARGS+=( --password-hash "${OUVRAGE_OWNER_PASSWORD_HASH}" )
    else
        MIGRATE_ARGS+=( --password "${OUVRAGE_OWNER_PASSWORD}" )
    fi
    python3 -m ouvrage migrate-auth "${MIGRATE_ARGS[@]}" \
        || echo "[entrypoint] migrate-auth failed (non-fatal, may already exist)"
fi

# --- Lock down secrets — copy to service-user-only location ---
# /run/secrets/ may be read-only (bind mount :ro), so we copy secrets to
# /data/.secrets/ owned by switchboard-svc with mode 400. Worker user can't read.
# Python helpers read from /data/.secrets/ first, then /run/secrets/ fallback.
SECURE_DIR="/data/.secrets"
mkdir -p "$SECURE_DIR"

SECRET_FILE="/run/secrets/master_key"
if [ -f "$SECRET_FILE" ]; then
    cp "$SECRET_FILE" "$SECURE_DIR/master_key"
    chown switchboard-svc "$SECURE_DIR/master_key"
    chmod 400 "$SECURE_DIR/master_key"
    echo "[entrypoint] Master key locked to service user"
fi
OPENAI_SECRET="/run/secrets/openai_key"
if [ -f "$OPENAI_SECRET" ]; then
    cp "$OPENAI_SECRET" "$SECURE_DIR/openai_key"
    chown switchboard-svc "$SECURE_DIR/openai_key"
    chmod 400 "$SECURE_DIR/openai_key"
    echo "[entrypoint] OpenAI key locked to service user"
fi

# --- Fix /data ownership after any file creation above ---
chown -R switchboard-svc:switchboard /data

# --- Lock down /data from worker user ---
# Worker (switchboard) is in group switchboard, but /data should only be
# accessible to the service user. Remove group/other permissions entirely.
chmod 700 /data
find /data -type f -exec chmod 600 {} +

# --- Grant capabilities to Python so they survive the user drop ---
# setuid/setgid/kill needed for spawning CC workers as the switchboard user
PYTHON_BIN=$(readlink -f "$(which python3)")
setcap 'cap_setuid,cap_setgid,cap_kill+eip' "$PYTHON_BIN"

# --- Drop to service user and start the app ---
exec gosu switchboard-svc "$@"
