"""Tests for POST /dashboard/api/components endpoint."""

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


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPostComponents:

    async def test_create_component_success(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/components", method="POST")
        resp = _Capture()
        payload = {
            "id": "my-feature",
            "project_id": sample_project["id"],
            "name": "My Feature",
            "description": "A test component",
        }
        await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 201
        data = resp.json()
        assert data["id"] == "my-feature"
        assert data["name"] == "My Feature"
        assert data["description"] == "A test component"
        assert data["project_id"] == sample_project["id"]

    async def test_create_component_no_description(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/components", method="POST")
        resp = _Capture()
        payload = {
            "id": "no-desc",
            "project_id": sample_project["id"],
            "name": "No Description",
        }
        await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 201
        data = resp.json()
        assert data["id"] == "no-desc"
        assert data["name"] == "No Description"

    async def test_create_component_persisted_in_db(self, db, sample_project):
        from switchboard.dashboard.api import handle_request
        import switchboard.db as sw_db

        scope = _make_scope("/dashboard/api/components", method="POST")
        resp = _Capture()
        payload = {
            "id": "persisted-comp",
            "project_id": sample_project["id"],
            "name": "Persisted",
        }
        await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 201
        comp = await sw_db.get_component("persisted-comp")
        assert comp is not None
        assert comp["name"] == "Persisted"

    async def test_create_component_missing_id(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/components", method="POST")
        resp = _Capture()
        payload = {"project_id": sample_project["id"], "name": "No ID"}
        await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 400
        assert "id is required" in resp.json()["error"]

    async def test_create_component_missing_project_id(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/components", method="POST")
        resp = _Capture()
        payload = {"id": "comp-id", "name": "No Project"}
        await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 400
        assert "project_id is required" in resp.json()["error"]

    async def test_create_component_missing_name(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        scope = _make_scope("/dashboard/api/components", method="POST")
        resp = _Capture()
        payload = {"id": "comp-id", "project_id": sample_project["id"]}
        await handle_request(scope, _make_receive(payload), resp)

        assert resp.status == 400
        assert "name is required" in resp.json()["error"]

    async def test_create_component_appears_in_list(self, db, sample_project):
        from switchboard.dashboard.api import handle_request

        project_id = sample_project["id"]

        # Create
        scope = _make_scope("/dashboard/api/components", method="POST")
        resp = _Capture()
        await handle_request(scope, _make_receive({
            "id": "listed-comp",
            "project_id": project_id,
            "name": "Listed",
        }), resp)
        assert resp.status == 201

        # List
        list_scope = _make_scope(
            "/dashboard/api/components",
            method="GET",
        )
        list_scope["query_string"] = f"project_id={project_id}".encode()
        list_resp = _Capture()
        await handle_request(list_scope, _make_receive(), list_resp)

        assert list_resp.status == 200
        components = list_resp.json()
        ids = [c["id"] for c in components]
        assert "listed-comp" in ids
