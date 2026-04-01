import os
import threading

# Store exit status so pytest_unconfigure can use it after reporting is done
_final_exitstatus = 0

def pytest_sessionfinish(session, exitstatus):
    global _final_exitstatus
    _final_exitstatus = exitstatus

def pytest_unconfigure(config):
    """Kill leaked threads AFTER pytest prints its full summary (including -rFE)."""
    alive = [t for t in threading.enumerate()
             if t.is_alive() and t is not threading.main_thread() and not t.daemon]
    if alive:
        print(f"\n⚠️  {len(alive)} non-daemon threads blocking exit:")
        for t in alive:
            print(f"  - {t.name} (daemon={t.daemon})")
        # Force exit — don't let leaked aiosqlite threads hold the process hostage
        os._exit(_final_exitstatus)
