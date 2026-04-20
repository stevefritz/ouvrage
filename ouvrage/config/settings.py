"""Environment variable reads and runtime settings for Ouvrage."""

import os

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# LOG_DIR — directory for rotating log files.
# Set to "" or unset to use the default: /opt/ouvrage/logs
# File: {LOG_DIR}/ouvrage.log (10 MB × 5 backups, DEBUG level)
LOG_DIR = os.environ.get("LOG_DIR", "/opt/ouvrage/logs")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("OUVRAGE_DB", "./data/ouvrage.db")

# ---------------------------------------------------------------------------
# File Uploads
# ---------------------------------------------------------------------------

UPLOADS_DIR = os.environ.get("UPLOADS_DIR", "/work/.uploads")

# ---------------------------------------------------------------------------
# Crash Recovery
# ---------------------------------------------------------------------------

RECOVERY_STAGGER_SECONDS = int(os.environ.get("RECOVERY_STAGGER_SECONDS", "30"))
MAX_RECOVERY_ATTEMPTS = int(os.environ.get("MAX_RECOVERY_ATTEMPTS", "3"))
RECOVERY_ENABLED = os.environ.get("RECOVERY_ENABLED", "true").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

WORKER_USER = os.environ.get("WORKER_USER", "switchboard")

# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")

# ---------------------------------------------------------------------------
# Web Push / VAPID
# ---------------------------------------------------------------------------

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY")
VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "mailto:admin@example.com")

# ---------------------------------------------------------------------------
# Auth (OAuth 2.1 resource server)
# ---------------------------------------------------------------------------

AUTH_ISSUER_URL = os.environ.get("AUTH_ISSUER_URL")  # e.g. https://auth.example.dev
AUTH_AUDIENCE = os.environ.get("AUTH_AUDIENCE")  # e.g. https://ouvrage.example.dev/mcp
AUTH_REQUIRED_SCOPES = os.environ.get("AUTH_REQUIRED_SCOPES", "").split(",") if os.environ.get("AUTH_REQUIRED_SCOPES") else []
RESOURCE_URL = os.environ.get("RESOURCE_URL")  # e.g. https://ouvrage.example.dev/mcp

# ---------------------------------------------------------------------------
# SaaS / Auth Mode
# ---------------------------------------------------------------------------

# AUTH_MODE controls how unauthenticated dashboard requests are handled.
# "local" (default): no-session → 401/redirect to local login page.
# "saas": no-session → 302 redirect to CONTROL_PLANE_URL/login for SSO.
AUTH_MODE = os.environ.get("AUTH_MODE", "local")

# CONTROL_PLANE_URL is required when AUTH_MODE=saas.
# Example: https://ouvrage.build
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL")

# CONTROL_PLANE_JWKS is the URL to fetch the control plane's public JWKS.
# Example: https://ouvrage.build/api/auth/.well-known/jwks.json
CONTROL_PLANE_JWKS = os.environ.get("CONTROL_PLANE_JWKS")

# INSTANCE_SLUG is this instance's slug, used as the expected JWT audience.
# Example: my-tenant
INSTANCE_SLUG = os.environ.get("INSTANCE_SLUG")

# INTERNAL_API_TOKEN is the Bearer token for machine-to-machine /internal/* endpoints.
# Required when AUTH_MODE=saas. Shared with the control plane at container startup.
INTERNAL_API_TOKEN = os.environ.get("INTERNAL_API_TOKEN")

# MAX_PROJECTS — max number of projects allowed (0 = unlimited, backward-compatible default).
# Can be overridden at runtime via POST /internal/config.
MAX_PROJECTS = int(os.environ.get("MAX_PROJECTS", "0"))

# ---------------------------------------------------------------------------
# OAuth Authorization Server
# ---------------------------------------------------------------------------

OAUTH_BASE_URL = os.environ.get("OAUTH_BASE_URL")  # e.g. https://ouvrage.example.dev
OAUTH_RSA_KEY_PATH = os.environ.get("OAUTH_RSA_KEY_PATH", "./data/oauth_rsa_key.pem")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET")  # claude-mcp client secret (seeded on first run)

# ---------------------------------------------------------------------------
# Credential Check Bypass
# ---------------------------------------------------------------------------

# SKIP_CREDENTIAL_CHECK — when true, dispatch_task skips the Anthropic API key
# check and create_project skips the PAT-exists check (clone validation still
# runs if a PAT IS configured).  Explicit opt-in only — must be set in env.
SKIP_CREDENTIAL_CHECK = os.environ.get("SKIP_CREDENTIAL_CHECK", "false").lower() in ("true", "1", "yes")
