#!/bin/bash
set -euo pipefail

# --- Fix volume ownership ---
# Mounted volumes start as root. Fix ownership so the app and workers can write.
chown switchboard-svc:switchboard /data
chown switchboard:switchboard /work

# --- Master key resolution ---
# Priority: Docker secret file (secure) > env var (bare metal) > generate + warn
SECRET_FILE="/run/secrets/master_key"
if [ -f "$SECRET_FILE" ]; then
    # Python reads directly from file via crypto.get_master_key()
    # migrate-auth runs as root (before gosu) so it can still read the file
    # After this block, entrypoint chowns to service user so worker can't read it
    echo "[entrypoint] Master key found at Docker secret"
elif [ -z "${SWITCHBOARD_MASTER_KEY:-}" ]; then
    export SWITCHBOARD_MASTER_KEY
    SWITCHBOARD_MASTER_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    echo "[entrypoint] WARNING: No /run/secrets/master_key or SWITCHBOARD_MASTER_KEY env var found"
    echo "[entrypoint] Generated ephemeral key — credentials will be unrecoverable if container restarts"
    echo "[entrypoint] Set SWITCHBOARD_MASTER_KEY or use Docker secrets for production"
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
if [ -n "${SWITCHBOARD_OWNER_EMAIL:-}" ] && [ -n "${SWITCHBOARD_OWNER_PASSWORD_HASH:-}" ]; then
    echo "[entrypoint] Running migrate-auth..."
    python3 -m switchboard migrate-auth \
        --email "${SWITCHBOARD_OWNER_EMAIL}" \
        --name "${SWITCHBOARD_OWNER_NAME:-Owner}" \
        --password-hash "${SWITCHBOARD_OWNER_PASSWORD_HASH}" \
        --slug "${SWITCHBOARD_INSTANCE_SLUG:-default}" \
        --instance-name "${SWITCHBOARD_INSTANCE_NAME:-Foreman}" \
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

# --- Grant capabilities to Python so they survive the user drop ---
# setuid/setgid/kill needed for spawning CC workers as the switchboard user
PYTHON_BIN=$(readlink -f "$(which python3)")
setcap 'cap_setuid,cap_setgid,cap_kill+eip' "$PYTHON_BIN"

# --- Drop to service user and start the app ---
exec gosu switchboard-svc "$@"
