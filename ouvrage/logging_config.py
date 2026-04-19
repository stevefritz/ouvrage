"""Logging configuration for Ouvrage.

Sets up a RotatingFileHandler for persistent DEBUG-level logging to disk,
independent of journalctl/stdout. The console handler stays at INFO.

Call configure_logging() once at startup (before db.init_db()) in main().
"""

import logging
import logging.handlers
import os
import sys


def configure_logging() -> None:
    """Configure file-based logging with rotation, separate from journalctl/stdout.

    File handler:    DEBUG  → {LOG_DIR}/ouvrage.log (10 MB × 5 backups)
    Console handler: INFO   → stdout (for journalctl / systemd)

    The 'ouvrage' logger is set to DEBUG so all messages reach the file
    handler. The console handler filters to INFO for normal operations.
    propagate=False prevents double output via uvicorn's root handler.

    Idempotent: safe to call multiple times (clears handlers before adding).
    """
    log_dir = os.environ.get("LOG_DIR", "/opt/ouvrage/logs")
    log_file = os.path.join(log_dir, "ouvrage.log")

    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as e:
        print(
            f"[ouvrage] Warning: cannot create log directory {log_dir!r}: {e}",
            file=sys.stderr,
        )
        log_file = None

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    logger = logging.getLogger("ouvrage")
    logger.setLevel(logging.DEBUG)
    # Clear existing handlers to stay idempotent (e.g. called twice in tests)
    logger.handlers.clear()

    # --- File handler (DEBUG, rotating) ---
    if log_file is not None:
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    # --- Console handler (INFO, for journalctl) ---
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    # Don't propagate to root to avoid double output via uvicorn's handler
    logger.propagate = False
