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
