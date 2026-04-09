"""Verify the aiosqlite daemon-thread monkey-patch in tests/conftest.py.

The patch makes aiosqlite worker threads daemons so that leaked connections
don't block pytest from exiting. Without this, a single test that forgets to
close a connection would hang the entire suite indefinitely.

These tests intentionally leak connections to prove:
  1. The monkey-patch is in effect (thread is daemon)
  2. Leaked threads don't prevent pytest from terminating
"""

import aiosqlite
import pytest


class TestAiosqliteDaemonPatch:
    async def test_aiosqlite_thread_is_daemon(self, tmp_path):
        """A freshly opened aiosqlite connection's worker thread should be a daemon."""
        db_path = tmp_path / "leak-test.db"
        conn = await aiosqlite.connect(str(db_path))
        try:
            assert conn._thread is not None, "aiosqlite connection should have a worker thread"
            assert conn._thread.is_alive(), "worker thread should be running"
            assert conn._thread.daemon is True, (
                "worker thread must be a daemon — the conftest monkey-patch is broken. "
                "Without daemon=True, leaked connections will hang pytest."
            )
        finally:
            await conn.close()

    async def test_intentionally_leaked_connection_does_not_block(self, tmp_path):
        """Intentionally leak a connection and confirm its thread is still daemon.

        We can't directly test 'pytest exits cleanly' from inside a test, but we
        can prove the leaked thread won't block exit by checking it's a daemon.
        Daemon threads are killed automatically when the main thread terminates.
        """
        db_path = tmp_path / "leak2.db"
        conn = await aiosqlite.connect(str(db_path))
        # Deliberately do NOT close the connection.
        # Without the monkey-patch, this would leave a non-daemon thread alive
        # and pytest would hang at the end of the session.
        assert conn._thread.daemon is True
        # No conn.close() — the thread leaks. The patch makes this safe.
