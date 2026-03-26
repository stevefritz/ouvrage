"""Compatibility shim — redirects 'web_push' to switchboard.notifications.web_push.

By replacing sys.modules['web_push'] with the real module, any monkeypatching
of 'web_push.VAPID_PRIVATE_KEY' etc. in tests will affect the actual module
that is_enabled() and _send_one() read from.
"""
import sys
import switchboard.notifications.web_push as _real

sys.modules[__name__] = _real
