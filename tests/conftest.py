import threading
from unittest.mock import MagicMock

import aiosqlite

# Monkey-patch aiosqlite so its worker thread is created as a daemon thread.
# This prevents leaked connections from blocking process exit in tests.
# In-memory test DBs are thrown away anyway, so clean shutdown is irrelevant.
_orig_aiosqlite_init = aiosqlite.Connection.__init__


def _daemon_aiosqlite_init(self, *args, **kwargs):
    _orig_aiosqlite_init(self, *args, **kwargs)
    self._thread.daemon = True


aiosqlite.Connection.__init__ = _daemon_aiosqlite_init


def pytest_unconfigure(config):
    """Warn about any remaining non-daemon threads (diagnostic only)."""
    alive = [t for t in threading.enumerate()
             if t.is_alive() and t is not threading.main_thread() and not t.daemon]
    if alive:
        print(f"\n⚠️  {len(alive)} non-daemon threads leaked:")
        for t in alive:
            print(f"  - {t.name} (daemon={t.daemon})")


import pytest


@pytest.fixture(autouse=True)
def disable_notifications(monkeypatch):
    monkeypatch.setattr('ouvrage.notifications.web_push.get_executor', lambda: MagicMock())
