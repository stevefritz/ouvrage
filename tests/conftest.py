import threading
from unittest.mock import MagicMock


def pytest_unconfigure(config):
    """Warn about leaked non-daemon threads."""
    alive = [t for t in threading.enumerate()
             if t.is_alive() and t is not threading.main_thread() and not t.daemon]
    if alive:
        print(f"\n⚠️  {len(alive)} non-daemon threads blocking exit:")
        for t in alive:
            print(f"  - {t.name} (daemon={t.daemon})")


import pytest


@pytest.fixture(autouse=True)
def disable_notifications(monkeypatch):
    monkeypatch.setattr('switchboard.notifications.web_push.get_executor', lambda: MagicMock())
