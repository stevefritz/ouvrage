"""Tests for the files management API endpoints and MCP list_files tool.

Covers:
- POST /dashboard/api/files — upload, type validation, size limit
- GET /dashboard/api/files — list files
- PATCH /dashboard/api/files/{id} — rename
- DELETE /dashboard/api/files/{id} — delete
- MCP list_files tool
"""

import io
import json
import shutil
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import switchboard.db as db
from switchboard.dashboard.api import handle_request


# ── ASGI helpers ──────────────────────────────────────────────────────────────


def _make_scope(
    path: str,
    method: str = "GET",
    headers: list | None = None,
    user_id: int = 1,
) -> dict:
    default_headers = []
    if headers:
        default_headers.extend(headers)
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": default_headers,
        "session_user": {"id": user_id, "email": "owner@localhost", "name": "Owner", "role": "owner"},
    }


def _make_receive(body: bytes = b""):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
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


def _make_multipart(filename: str, file_data: bytes, field_name: str = "file") -> tuple[bytes, bytes]:
    """Build a minimal multipart/form-data body. Returns (body, boundary)."""
    boundary = b"testboundary1234567890"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="' + field_name.encode() + b'"; filename="' + filename.encode() + b'"\r\n'
        b"Content-Type: application/octet-stream\r\n"
        b"\r\n" + file_data + b"\r\n"
        b"--" + boundary + b"--\r\n"
    )
    return body, boundary


def _upload_scope(filename: str, body: bytes, boundary: bytes, user_id: int = 1) -> dict:
    ct = b"multipart/form-data; boundary=" + boundary
    return {
        "type": "http",
        "method": "POST",
        "path": "/dashboard/api/files",
        "query_string": b"",
        "headers": [
            (b"content-type", ct),
            (b"content-length", str(len(body)).encode()),
        ],
        "session_user": {"id": user_id, "email": "owner@localhost", "name": "Owner", "role": "owner"},
    }


# ── Upload directory fixture ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def tmp_uploads(tmp_path, monkeypatch):
    """Redirect ~/uploads to a temp directory for all file tests."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()

    # Patch Path.home() to return tmp_path
    original_home = Path.home

    def fake_home():
        return tmp_path

    monkeypatch.setattr(Path, "home", staticmethod(fake_home))
    yield uploads
    # Cleanup is automatic via tmp_path


# ── GET /dashboard/api/files ───────────────────────────────────────────────


class TestListFiles:

    async def test_list_empty(self, db):
        scope = _make_scope("/dashboard/api/files", "GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        assert resp.json() == []

    async def test_list_returns_inserted_files(self, db):
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid,
            filename="test.png",
            stored_path="/tmp/test/test.png",
            mime_type="image/png",
            size_bytes=1234,
            uploaded_by=None,
        )
        scope = _make_scope("/dashboard/api/files", "GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        files = resp.json()
        assert len(files) == 1
        assert files[0]["id"] == fid
        assert files[0]["filename"] == "test.png"

    async def test_list_requires_auth(self, db):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/dashboard/api/files",
            "query_string": b"",
            "headers": [],
            # No session_user
        }
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        # Unauthenticated scope has no session_user, list should still work
        # (list doesn't require auth per spec — only upload/rename/delete do)
        assert resp.status == 200


# ── POST /dashboard/api/files ─────────────────────────────────────────────


class TestUploadFile:

    async def test_upload_png(self, db, tmp_uploads):
        file_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        body, boundary = _make_multipart("photo.png", file_data)
        scope = _upload_scope("photo.png", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 201
        data = resp.json()
        assert data["filename"] == "photo.png"
        assert data["mime_type"] == "image/png"
        assert data["size_bytes"] == len(file_data)
        assert "id" in data
        assert Path(data["stored_path"]).exists()

    async def test_upload_text_file(self, db, tmp_uploads):
        file_data = b"hello, world"
        body, boundary = _make_multipart("notes.txt", file_data)
        scope = _upload_scope("notes.txt", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 201
        data = resp.json()
        assert data["mime_type"] == "text/plain"

    async def test_upload_pdf(self, db, tmp_uploads):
        file_data = b"%PDF-1.4"
        body, boundary = _make_multipart("report.pdf", file_data)
        scope = _upload_scope("report.pdf", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 201

    async def test_upload_requires_auth(self, db, tmp_uploads):
        file_data = b"data"
        body, boundary = _make_multipart("test.txt", file_data)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/dashboard/api/files",
            "query_string": b"",
            "headers": [
                (b"content-type", b"multipart/form-data; boundary=" + boundary),
            ],
            # No session_user
        }
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 401

    async def test_upload_stores_in_db(self, db, tmp_uploads):
        file_data = b"content"
        body, boundary = _make_multipart("doc.md", file_data)
        scope = _upload_scope("doc.md", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 201
        file_id = resp.json()["id"]
        record = await db.get_file(file_id)
        assert record is not None
        assert record["filename"] == "doc.md"

    async def test_upload_file_saved_to_uuid_subdir(self, db, tmp_uploads):
        file_data = b"x" * 50
        body, boundary = _make_multipart("data.json", file_data)
        scope = _upload_scope("data.json", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 201
        data = resp.json()
        stored = Path(data["stored_path"])
        # Parent should be UUID dir, grandparent should be uploads dir
        assert stored.parent.parent == tmp_uploads
        assert stored.name == "data.json"


# ── Type validation ────────────────────────────────────────────────────────


class TestFileTypeValidation:

    async def test_reject_exe(self, db, tmp_uploads):
        body, boundary = _make_multipart("virus.exe", b"MZ")
        scope = _upload_scope("virus.exe", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 400
        assert ".exe" in resp.json()["error"]
        assert "not allowed" in resp.json()["error"]

    async def test_reject_zip(self, db, tmp_uploads):
        body, boundary = _make_multipart("archive.zip", b"PK")
        scope = _upload_scope("archive.zip", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 400

    async def test_reject_no_extension(self, db, tmp_uploads):
        body, boundary = _make_multipart("Makefile", b"all: test")
        scope = _upload_scope("Makefile", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 400

    async def test_allowed_extensions(self, db, tmp_uploads):
        for ext in ["png", "jpg", "gif", "txt", "md", "json", "csv", "pdf"]:
            body, boundary = _make_multipart(f"file.{ext}", b"data")
            scope = _upload_scope(f"file.{ext}", body, boundary)
            resp = _Capture()
            await handle_request(scope, _make_receive(body), resp)
            assert resp.status == 201, f"Expected 201 for .{ext}, got {resp.status}"


# ── Size validation ────────────────────────────────────────────────────────


class TestFileSizeValidation:

    async def test_reject_oversized_via_content_length(self, db, tmp_uploads):
        # Send a Content-Length header claiming > 10MB
        boundary = b"testboundary"
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/dashboard/api/files",
            "query_string": b"",
            "headers": [
                (b"content-type", b"multipart/form-data; boundary=" + boundary),
                (b"content-length", b"10485761"),  # 10MB + 1
            ],
            "session_user": {"id": 1, "email": "owner@localhost", "name": "Owner", "role": "owner"},
        }
        resp = _Capture()
        await handle_request(scope, _make_receive(b""), resp)
        assert resp.status == 413
        assert "10MB" in resp.json()["error"]

    async def test_reject_oversized_actual_body(self, db, tmp_uploads):
        # Body is exactly over limit
        big_data = b"x" * (10 * 1024 * 1024 + 1)
        body, boundary = _make_multipart("big.txt", big_data)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/dashboard/api/files",
            "query_string": b"",
            "headers": [
                (b"content-type", b"multipart/form-data; boundary=" + boundary),
            ],
            "session_user": {"id": 1, "email": "owner@localhost", "name": "Owner", "role": "owner"},
        }
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 413

    async def test_accept_under_10mb(self, db, tmp_uploads):
        # Just under 10MB should be accepted (content-length header includes multipart framing)
        data = b"x" * (10 * 1024 * 1024 - 500)
        body, boundary = _make_multipart("max.txt", data)
        scope = _upload_scope("max.txt", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 201


# ── PATCH /dashboard/api/files/{id} ───────────────────────────────────────


class TestRenameFile:

    async def _insert_file(self, tmp_uploads) -> tuple[str, Path]:
        """Insert a test file record with a real file on disk."""
        fid = str(uuid.uuid4())
        uuid_dir = tmp_uploads / fid
        uuid_dir.mkdir()
        dest = uuid_dir / "original.txt"
        dest.write_bytes(b"hello")
        await db.create_file(
            id=fid,
            filename="original.txt",
            stored_path=str(dest),
            mime_type="text/plain",
            size_bytes=5,
            uploaded_by=1,
        )
        return fid, dest

    async def test_rename_updates_db(self, db, tmp_uploads):
        fid, _ = await self._insert_file(tmp_uploads)
        body = json.dumps({"filename": "renamed.txt"}).encode()
        scope = _make_scope(f"/dashboard/api/files/{fid}", "PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 200
        data = resp.json()
        assert data["filename"] == "renamed.txt"

    async def test_rename_moves_file_on_disk(self, db, tmp_uploads):
        fid, old_path = await self._insert_file(tmp_uploads)
        body = json.dumps({"filename": "moved.txt"}).encode()
        scope = _make_scope(f"/dashboard/api/files/{fid}", "PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 200
        assert not old_path.exists()
        new_path = old_path.parent / "moved.txt"
        assert new_path.exists()

    async def test_rename_requires_auth(self, db, tmp_uploads):
        fid, _ = await self._insert_file(tmp_uploads)
        body = json.dumps({"filename": "x.txt"}).encode()
        scope = {
            "type": "http",
            "method": "PATCH",
            "path": f"/dashboard/api/files/{fid}",
            "query_string": b"",
            "headers": [],
        }
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 401

    async def test_rename_not_found(self, db, tmp_uploads):
        body = json.dumps({"filename": "x.txt"}).encode()
        scope = _make_scope("/dashboard/api/files/nonexistent-id", "PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 404

    async def test_rename_rejects_bad_extension(self, db, tmp_uploads):
        fid, _ = await self._insert_file(tmp_uploads)
        body = json.dumps({"filename": "file.exe"}).encode()
        scope = _make_scope(f"/dashboard/api/files/{fid}", "PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 400


# ── DELETE /dashboard/api/files/{id} ──────────────────────────────────────


class TestDeleteFile:

    async def _insert_file(self, tmp_uploads) -> tuple[str, Path]:
        fid = str(uuid.uuid4())
        uuid_dir = tmp_uploads / fid
        uuid_dir.mkdir()
        dest = uuid_dir / "delete_me.txt"
        dest.write_bytes(b"bye")
        await db.create_file(
            id=fid,
            filename="delete_me.txt",
            stored_path=str(dest),
            mime_type="text/plain",
            size_bytes=3,
            uploaded_by=1,
        )
        return fid, dest

    async def test_delete_removes_from_db(self, db, tmp_uploads):
        fid, _ = await self._insert_file(tmp_uploads)
        scope = _make_scope(f"/dashboard/api/files/{fid}", "DELETE")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        assert await db.get_file(fid) is None

    async def test_delete_removes_from_disk(self, db, tmp_uploads):
        fid, file_path = await self._insert_file(tmp_uploads)
        uuid_dir = file_path.parent
        scope = _make_scope(f"/dashboard/api/files/{fid}", "DELETE")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        assert not uuid_dir.exists()

    async def test_delete_requires_auth(self, db, tmp_uploads):
        fid, _ = await self._insert_file(tmp_uploads)
        scope = {
            "type": "http",
            "method": "DELETE",
            "path": f"/dashboard/api/files/{fid}",
            "query_string": b"",
            "headers": [],
        }
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 401

    async def test_delete_not_found(self, db, tmp_uploads):
        scope = _make_scope("/dashboard/api/files/nonexistent", "DELETE")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 404


# ── MCP list_files tool ────────────────────────────────────────────────────


class TestListFilesTool:

    async def test_list_files_empty(self, db):
        from switchboard.server.handlers.files_handler import _handle_list_files
        result = await _handle_list_files({})
        assert result == {"files": []}

    async def test_list_files_returns_records(self, db):
        from switchboard.server.handlers.files_handler import _handle_list_files
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid,
            filename="ref.pdf",
            stored_path="/home/user/uploads/abc/ref.pdf",
            mime_type="application/pdf",
            size_bytes=999,
            uploaded_by=None,
        )
        result = await _handle_list_files({})
        assert len(result["files"]) == 1
        assert result["files"][0]["stored_path"] == "/home/user/uploads/abc/ref.pdf"
        assert result["files"][0]["filename"] == "ref.pdf"
