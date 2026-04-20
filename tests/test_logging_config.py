"""Tests for ouvrage.logging_config — RotatingFileHandler setup."""

import logging
import logging.handlers
import os

import pytest


class TestConfigureLogging:
    @pytest.fixture(autouse=True)
    def reset_logger(self):
        """Reset the ouvrage logger after each test."""
        logger = logging.getLogger("ouvrage")
        original_handlers = logger.handlers[:]
        original_level = logger.level
        original_propagate = logger.propagate
        yield
        # Close all handlers before replacing to avoid ResourceWarning
        for h in logger.handlers:
            h.close()
        logger.handlers = original_handlers
        logger.level = original_level
        logger.propagate = original_propagate

    def test_file_handler_created(self, tmp_path, monkeypatch):
        """configure_logging() adds a RotatingFileHandler."""
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        from ouvrage.logging_config import configure_logging
        configure_logging()
        logger = logging.getLogger("ouvrage")
        file_handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(file_handlers) == 1

    def test_file_handler_path(self, tmp_path, monkeypatch):
        """File handler writes to LOG_DIR/ouvrage.log."""
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        from ouvrage.logging_config import configure_logging
        configure_logging()
        logger = logging.getLogger("ouvrage")
        fh = next(
            h for h in logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        )
        assert fh.baseFilename == str(tmp_path / "ouvrage.log")

    def test_rotation_config(self, tmp_path, monkeypatch):
        """Rotation configured for 10 MB max size and 5 backups."""
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        from ouvrage.logging_config import configure_logging
        configure_logging()
        logger = logging.getLogger("ouvrage")
        fh = next(
            h for h in logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        )
        assert fh.maxBytes == 10 * 1024 * 1024
        assert fh.backupCount == 5

    def test_file_handler_level_debug(self, tmp_path, monkeypatch):
        """File handler captures DEBUG and above."""
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        from ouvrage.logging_config import configure_logging
        configure_logging()
        logger = logging.getLogger("ouvrage")
        fh = next(
            h for h in logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        )
        assert fh.level == logging.DEBUG

    def test_console_handler_level_info(self, tmp_path, monkeypatch):
        """Console handler captures INFO and above only."""
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        from ouvrage.logging_config import configure_logging
        configure_logging()
        logger = logging.getLogger("ouvrage")
        stream_handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(stream_handlers) == 1
        assert stream_handlers[0].level == logging.INFO

    def test_logger_level_debug(self, tmp_path, monkeypatch):
        """Ouvrage logger itself is set to DEBUG."""
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        from ouvrage.logging_config import configure_logging
        configure_logging()
        assert logging.getLogger("ouvrage").level == logging.DEBUG

    def test_propagate_false(self, tmp_path, monkeypatch):
        """Ouvrage logger does not propagate to root (no double output via uvicorn)."""
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        from ouvrage.logging_config import configure_logging
        configure_logging()
        assert logging.getLogger("ouvrage").propagate is False

    def test_log_dir_created(self, tmp_path, monkeypatch):
        """configure_logging() creates the log directory if missing."""
        new_dir = tmp_path / "logs" / "sub"
        monkeypatch.setenv("LOG_DIR", str(new_dir))
        from ouvrage.logging_config import configure_logging
        configure_logging()
        assert new_dir.exists()

    def test_idempotent(self, tmp_path, monkeypatch):
        """Calling configure_logging() twice doesn't add duplicate handlers."""
        monkeypatch.setenv("LOG_DIR", str(tmp_path))
        from ouvrage.logging_config import configure_logging
        configure_logging()
        configure_logging()
        logger = logging.getLogger("ouvrage")
        file_handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(file_handlers) == 1

    def test_missing_log_dir_falls_back_to_console_only(self, tmp_path, monkeypatch):
        """If log dir can't be created, only a console handler is added."""
        # Point to a path that can't be created (a file is in the way)
        blocker = tmp_path / "blocker"
        blocker.write_text("I am a file")
        bad_dir = str(blocker / "logs")  # can't mkdir inside a file
        monkeypatch.setenv("LOG_DIR", bad_dir)
        from ouvrage.logging_config import configure_logging
        configure_logging()
        logger = logging.getLogger("ouvrage")
        file_handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        stream_handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(file_handlers) == 0
        assert len(stream_handlers) == 1
