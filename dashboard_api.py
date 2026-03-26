"""Compatibility shim — dashboard_api.py now lives at switchboard/dashboard/api.py."""

import switchboard.db as db  # noqa: F401 — tests patch dashboard_api.db.*

from switchboard.dashboard.api import (  # noqa: F401
    _resolve_dashboard_log_dir,
    handle_request,
)
