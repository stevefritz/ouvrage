"""Tests for the runtime info endpoint.

Covers:
  GET /dashboard/api/runtime-info — returns list of installed runtimes
"""

import json
from unittest.mock import AsyncMock, patch

import pytest


# ── ASGI test helpers ─────────────────────────────────────────────────────────

def _make_scope(path: str, method: str = "GET", user_id: int = 1) -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "session_user": {"id": user_id, "email": "owner@localhost", "name": "Owner", "role": "owner"},
    }


def _make_receive(body=None):
    if body is None:
        raw = b""
    elif isinstance(body, dict):
        raw = json.dumps(body).encode()
    else:
        raw = body

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return receive


class _Capture:
    def __init__(self):
        self.status = None
        self.body = b""

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")

    def json(self):
        return json.loads(self.body)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_run_version(outputs: dict):
    """Return an async mock for _run_version_cmd that returns predefined outputs."""
    from switchboard.dashboard.api import _RUNTIME_COMMANDS

    async def fake_run(args):
        cmd = args[0]
        return outputs.get(cmd, "")

    return fake_run


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRuntimeInfo:

    async def test_returns_200(self, db):
        from switchboard.dashboard.api import handle_request

        with patch("switchboard.dashboard.api._run_version_cmd", new=AsyncMock(return_value="")):
            scope = _make_scope("/dashboard/api/runtime-info")
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        assert resp.status == 200

    async def test_returns_list(self, db):
        from switchboard.dashboard.api import handle_request

        with patch("switchboard.dashboard.api._run_version_cmd", new=AsyncMock(return_value="")):
            scope = _make_scope("/dashboard/api/runtime-info")
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        assert isinstance(data, list)
        assert len(data) > 0

    async def test_all_expected_runtimes_present(self, db):
        from switchboard.dashboard.api import handle_request

        with patch("switchboard.dashboard.api._run_version_cmd", new=AsyncMock(return_value="")):
            scope = _make_scope("/dashboard/api/runtime-info")
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        keys = {r["key"] for r in data}
        expected = {"python", "node", "typescript", "php", "ruby", "go", "rust", "java", "dotnet"}
        assert expected == keys

    async def test_each_entry_has_required_fields(self, db):
        from switchboard.dashboard.api import handle_request

        with patch("switchboard.dashboard.api._run_version_cmd", new=AsyncMock(return_value="")):
            scope = _make_scope("/dashboard/api/runtime-info")
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        for entry in data:
            assert "key" in entry
            assert "name" in entry
            assert "version" in entry
            assert "pkg_manager" in entry

    async def test_version_parsed_from_output(self, db):
        from switchboard.dashboard.api import handle_request, _RUNTIME_COMMANDS

        # Return realistic version strings for each command's first argument
        cmd_outputs = {
            "python3": "Python 3.13.0",
            "node": "v22.0.0",
            "tsc": "Version 5.4.5",
            "php": "PHP 8.3.4 (cli)",
            "ruby": "ruby 3.3.0 (2024-01-18 revision) [x86_64-linux]",
            "go": "go version go1.23.0 linux/amd64",
            "rustc": "rustc 1.78.0 (9b00956e5 2024-04-29)",
            "java": "openjdk version \"21.0.1\" 2023-10-17",
            "dotnet": "9.0.100",
        }

        async def fake_run(args):
            return cmd_outputs.get(args[0], "")

        with patch("switchboard.dashboard.api._run_version_cmd", new=fake_run):
            scope = _make_scope("/dashboard/api/runtime-info")
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        by_key = {r["key"]: r for r in data}

        assert by_key["python"]["version"] == "3.13.0"
        assert by_key["node"]["version"] == "22.0.0"
        assert by_key["go"]["version"] == "1.23.0"
        assert by_key["dotnet"]["version"] == "9.0.100"

    async def test_not_installed_when_empty_output(self, db):
        from switchboard.dashboard.api import handle_request

        with patch("switchboard.dashboard.api._run_version_cmd", new=AsyncMock(return_value="")):
            scope = _make_scope("/dashboard/api/runtime-info")
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        for entry in data:
            assert entry["version"] == "not installed"

    async def test_pkg_manager_values(self, db):
        from switchboard.dashboard.api import handle_request

        with patch("switchboard.dashboard.api._run_version_cmd", new=AsyncMock(return_value="")):
            scope = _make_scope("/dashboard/api/runtime-info")
            resp = _Capture()
            await handle_request(scope, _make_receive(), resp)

        data = resp.json()
        by_key = {r["key"]: r for r in data}

        assert by_key["python"]["pkg_manager"] == "pip"
        assert by_key["node"]["pkg_manager"] == "npm"
        assert by_key["rust"]["pkg_manager"] == "Cargo"
        assert by_key["java"]["pkg_manager"] == "Maven, Gradle"
        assert by_key["dotnet"]["pkg_manager"] == "dotnet CLI"
        assert by_key["go"]["pkg_manager"] is None
