"""Backward-compatible shim — web_push moved to switchboard.notifications.web_push.

We replace sys.modules['web_push'] with the real module so that patches like
    patch("web_push._send_one", ...)
    patch("web_push.db.get_notification_settings", ...)
    monkeypatch.setattr(web_push, "VAPID_PRIVATE_KEY", ...)
continue to work correctly against the real implementation.
"""
import sys
from switchboard.notifications import web_push as _wp

sys.modules[__name__] = _wp
