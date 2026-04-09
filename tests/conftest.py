import os as _os
import threading
from unittest.mock import MagicMock

_pytest_exit_status = 0


def pytest_sessionfinish(session, exitstatus):
    global _pytest_exit_status
    _pytest_exit_status = exitstatus


def pytest_unconfigure(config):
    """Warn about leaked non-daemon threads. Join with timeout so pytest exits cleanly."""
    alive = [t for t in threading.enumerate()
             if t.is_alive() and t is not threading.main_thread() and not t.daemon]
    if alive:
        print(f"\n⚠️  {len(alive)} non-daemon threads leaked — joining with 5s timeout:")
        for t in alive:
            print(f"  - {t.name} (daemon={t.daemon})")
            t.join(timeout=5)
        # If threads are still alive after join, force exit to prevent process hang.
        # Non-daemon threads would otherwise block Python's interpreter shutdown.
        still_alive = [t for t in threading.enumerate()
                       if t.is_alive() and t is not threading.main_thread() and not t.daemon]
        if still_alive:
            _os._exit(_pytest_exit_status)


import pytest


@pytest.fixture(autouse=True)
def disable_notifications(monkeypatch):
    monkeypatch.setattr('switchboard.notifications.web_push.get_executor', lambda: MagicMock())
