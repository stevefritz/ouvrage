import os
import threading


def pytest_sessionfinish(session, exitstatus):
    """Force exit if non-daemon threads would otherwise block process termination.

    ThreadPoolExecutor threads (e.g. web-push) are non-daemon and can keep the
    process alive for up to ~60s after tests complete, causing the gate's
    `timeout 220` to kill the process with exit_code=124 even when all tests pass.
    """
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
        os._exit(int(exitstatus))


def pytest_unconfigure(config):
    pass
