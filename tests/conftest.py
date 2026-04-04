import threading


def pytest_unconfigure(config):
    """Shut down web-push executor and warn about leaked threads."""
    try:
        from switchboard.notifications.web_push import _executor
        _executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass

    alive = [t for t in threading.enumerate()
             if t.is_alive() and t is not threading.main_thread() and not t.daemon]
    if alive:
        print(f"\n⚠️  {len(alive)} non-daemon threads blocking exit:")
        for t in alive:
            print(f"  - {t.name} (daemon={t.daemon})")
