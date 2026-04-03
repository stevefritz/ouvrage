"""Confirm that /dashboard/api/components and /dashboard/api/punchlist endpoints are removed."""

import json
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


# ── Tests: component endpoints return 404 ─────────────────────────────────────

class TestComponentEndpointsRemoved:

    async def test_get_components_returns_404(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/components", method="GET")
        scope["query_string"] = f"project_id={sample_project['id']}".encode()
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 404

    async def test_post_components_returns_404(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/components", method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive({
            "id": "my-feature",
            "project_id": sample_project["id"],
            "name": "My Feature",
        }), resp)
        assert resp.status == 404

    async def test_get_component_detail_returns_404(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/components/some-comp", method="GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 404

    async def test_patch_component_returns_404(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/components/some-comp", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"name": "New Name"}), resp)
        assert resp.status == 404

    async def test_component_pause_returns_404(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/components/some-comp/pause", method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 404

    async def test_component_stop_returns_404(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/components/some-comp/stop", method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 404


# ── Tests: punchlist endpoints return 404 ─────────────────────────────────────

class TestPunchlistEndpointsRemoved:

    async def test_get_punchlist_returns_404(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/punchlist/some-comp", method="GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 404

    async def test_post_punchlist_returns_404(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/punchlist/some-comp", method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive({"item": "Fix the button"}), resp)
        assert resp.status == 404

    async def test_patch_punchlist_item_returns_404(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/punchlist/some-comp/42", method="PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive({"status": "done"}), resp)
        assert resp.status == 404

    async def test_dispatch_punchlist_item_returns_404(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/punchlist/some-comp/42/dispatch", method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 404
