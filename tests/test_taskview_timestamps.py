"""Tests for TaskView smart timestamp formatting.

Verifies that:
- smartTime is exported from utils.js
- TaskView imports and uses smartTime for message and session log timestamps
- The old raw toLocaleTimeString calls are replaced
"""

import re
from pathlib import Path

UTILS_JS = Path(__file__).parent.parent / "dashboard" / "components" / "utils.js"
TASK_VIEW_JS = Path(__file__).parent.parent / "dashboard" / "views" / "TaskView.js"


class TestSmartTimeInUtils:
    def test_smart_time_exported(self):
        src = UTILS_JS.read_text()
        assert "export function smartTime" in src

    def test_smart_time_handles_null(self):
        src = UTILS_JS.read_text()
        # Function should guard against null/undefined input
        assert "if (!iso)" in src

    def test_smart_time_today_branch(self):
        src = UTILS_JS.read_text()
        # Should check if same calendar day
        assert "toDateString()" in src

    def test_smart_time_relative_branch(self):
        src = UTILS_JS.read_text()
        # Within 7 days: relative display using days
        assert "diffDays < 7" in src
        assert "d ago" in src

    def test_smart_time_date_branch(self):
        src = UTILS_JS.read_text()
        # Older: short date using toLocaleDateString
        assert "toLocaleDateString" in src
        assert "month" in src
        assert "day" in src


class TestTaskViewImportsSmartTime:
    def test_smart_time_imported(self):
        src = TASK_VIEW_JS.read_text()
        # Must import smartTime from utils
        assert "smartTime" in src
        import_line = next(
            (l for l in src.splitlines() if "from '../components/utils.js'" in l), ""
        )
        assert "smartTime" in import_line

    def test_message_list_uses_smart_time(self):
        src = TASK_VIEW_JS.read_text()
        # HaikuLine message timestamp should use smartTime
        assert "smartTime(msg.created_at)" in src

    def test_session_log_uses_smart_time(self):
        src = TASK_VIEW_JS.read_text()
        # Session log entry timestamp should use smartTime
        assert "smartTime(entry.timestamp)" in src

    def test_message_list_no_raw_locale_time(self):
        src = TASK_VIEW_JS.read_text()
        # The old raw toLocaleTimeString for msg.created_at must be gone
        assert "new Date(normTs(msg.created_at)).toLocaleTimeString" not in src

    def test_session_log_no_raw_locale_time(self):
        src = TASK_VIEW_JS.read_text()
        # The old raw toLocaleTimeString for entry.timestamp must be gone
        assert "new Date(entry.timestamp).toLocaleTimeString" not in src
