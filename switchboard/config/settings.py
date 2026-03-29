"""Environment variable reads and runtime settings for Switchboard."""

import os

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("SWITCHBOARD_DB", "./data/switchboard.db")

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
AUTH_AUDIENCE = os.environ.get("AUTH_AUDIENCE")  # e.g. https://switchboard.example.dev/mcp
AUTH_REQUIRED_SCOPES = os.environ.get("AUTH_REQUIRED_SCOPES", "").split(",") if os.environ.get("AUTH_REQUIRED_SCOPES") else []
RESOURCE_URL = os.environ.get("RESOURCE_URL")  # e.g. https://switchboard.example.dev/mcp

# ---------------------------------------------------------------------------
# SaaS / Auth Mode
# ---------------------------------------------------------------------------

# AUTH_MODE controls how unauthenticated dashboard requests are handled.
# "local" (default): no-session → 401/redirect to local login page.
# "saas": no-session → 302 redirect to CONTROL_PLANE_URL/login for SSO.
AUTH_MODE = os.environ.get("AUTH_MODE", "local")

# CONTROL_PLANE_URL is required when AUTH_MODE=saas.
# Example: https://foreman.dev
CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL")

# ---------------------------------------------------------------------------
# OAuth Authorization Server
# ---------------------------------------------------------------------------

OAUTH_BASE_URL = os.environ.get("OAUTH_BASE_URL")  # e.g. https://switchboard.example.dev
OAUTH_RSA_KEY_PATH = os.environ.get("OAUTH_RSA_KEY_PATH", "./data/oauth_rsa_key.pem")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET")  # claude-mcp client secret (seeded on first run)
