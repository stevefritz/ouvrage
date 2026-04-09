"""Tests for switchboard.logging_config — RotatingFileHandler setup."""

import logging
import logging.handlers
import os

import pytest


class TestConfigureLogging:
    @pytest.fixture(autouse=True)
    def reset_logger(self):
        """Reset the switchboard logger after each test."""
        logger = logging.getLogger("switchboard")
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
        from switchboard.logging_config import configure_logging
        configure_logging()
        logger = logging.getLogger("switchboard")
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
        from switchboard.logging_config import configure_logging
        configure_logging()
        logger = logging.getLogger("switchboard")
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
