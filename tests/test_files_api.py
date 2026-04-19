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

import ouvrage.db as db
from ouvrage.dashboard.api import handle_request


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


def _make_multipart_with_fields(filename: str, file_data: bytes, extra_fields: dict | None = None) -> tuple[bytes, bytes]:
    """Build a multipart/form-data body with optional extra form fields. Returns (body, boundary)."""
    boundary = b"testboundary1234567890"
    parts = b""
    # Add extra form fields first
    for field_name, field_value in (extra_fields or {}).items():
        parts += (
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="' + field_name.encode() + b'"\r\n'
            b"\r\n" + field_value.encode() + b"\r\n"
        )
    # Add file part
    parts += (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="file"; filename="' + filename.encode() + b'"\r\n'
        b"Content-Type: application/octet-stream\r\n"
        b"\r\n" + file_data + b"\r\n"
        b"--" + boundary + b"--\r\n"
    )
    return parts, boundary


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
    """Redirect uploads to a temp directory for all file tests."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()

    monkeypatch.setattr("ouvrage.config.settings.UPLOADS_DIR", str(uploads))
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

    async def test_upload_path_traversal_stripped(self, db, tmp_uploads):
        """Path traversal in upload filename is neutralized."""
        file_data = b"evil"
        body, boundary = _make_multipart("../../evil.txt", file_data)
        scope = _upload_scope("../../evil.txt", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 201
        data = resp.json()
        # filename should be stripped to just "evil.txt"
        assert data["filename"] == "evil.txt"
        stored = Path(data["stored_path"])
        # File must be inside the uploads dir, not escaped
        assert tmp_uploads in stored.parents


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

    async def test_rename_path_traversal_stripped(self, db, tmp_uploads):
        """Path traversal in rename filename is neutralized."""
        fid, old_path = await self._insert_file(tmp_uploads)
        body = json.dumps({"filename": "../../evil.txt"}).encode()
        scope = _make_scope(f"/dashboard/api/files/{fid}", "PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 200
        # Should have stripped to just "evil.txt" in the same UUID dir
        data = resp.json()
        assert data["filename"] == "evil.txt"
        assert Path(data["stored_path"]).parent == old_path.parent

    async def test_rename_updates_mime_type(self, db, tmp_uploads):
        """Renaming to a different extension updates mime_type in DB."""
        fid, _ = await self._insert_file(tmp_uploads)
        # Rename from .txt to .md — mime_type should update
        body = json.dumps({"filename": "renamed.md"}).encode()
        scope = _make_scope(f"/dashboard/api/files/{fid}", "PATCH")
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 200
        data = resp.json()
        assert data["mime_type"] == "text/markdown"


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


# ── GET /dashboard/api/files/{id}/download ────────────────────────────────


class _CaptureWithHeaders(_Capture):
    """Capture that also stores response headers."""
    def __init__(self):
        super().__init__()
        self.headers = {}

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            for k, v in message.get("headers", []):
                self.headers[k.decode()] = v.decode()
        await super().__call__(message)


class TestDownloadFile:

    async def _insert_file(self, tmp_uploads, content: bytes = b"file content") -> tuple[str, Path]:
        fid = str(uuid.uuid4())
        uuid_dir = tmp_uploads / fid
        uuid_dir.mkdir()
        dest = uuid_dir / "report.txt"
        dest.write_bytes(content)
        await db.create_file(
            id=fid,
            filename="report.txt",
            stored_path=str(dest),
            mime_type="text/plain",
            size_bytes=len(content),
            uploaded_by=1,
        )
        return fid, dest

    async def test_download_serves_file(self, db, tmp_uploads):
        content = b"hello world file content"
        fid, _ = await self._insert_file(tmp_uploads, content)
        scope = _make_scope(f"/dashboard/api/files/{fid}/download", "GET")
        resp = _CaptureWithHeaders()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        assert resp.body == content
        assert "text/plain" in resp.headers.get("content-type", "")
        assert "attachment" in resp.headers.get("content-disposition", "")
        assert "report.txt" in resp.headers.get("content-disposition", "")

    async def test_download_requires_auth(self, db, tmp_uploads):
        fid, _ = await self._insert_file(tmp_uploads)
        scope = {
            "type": "http",
            "method": "GET",
            "path": f"/dashboard/api/files/{fid}/download",
            "query_string": b"",
            "headers": [],
        }
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 401

    async def test_download_file_not_found(self, db, tmp_uploads):
        scope = _make_scope("/dashboard/api/files/nonexistent-id/download", "GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 404

    async def test_download_disk_file_missing(self, db, tmp_uploads):
        fid, dest = await self._insert_file(tmp_uploads)
        dest.unlink()  # Remove file from disk
        scope = _make_scope(f"/dashboard/api/files/{fid}/download", "GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 404

    async def test_download_sets_content_length(self, db, tmp_uploads):
        content = b"exactly this many bytes"
        fid, _ = await self._insert_file(tmp_uploads, content)
        scope = _make_scope(f"/dashboard/api/files/{fid}/download", "GET")
        resp = _CaptureWithHeaders()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        assert resp.headers.get("content-length") == str(len(content))


# ── MCP list_files tool ────────────────────────────────────────────────────


class TestListFilesTool:

    async def test_list_files_empty(self, db):
        from ouvrage.server.handlers.files_handler import _handle_list_files
        result = await _handle_list_files({})
        assert result == {"files": []}

    async def test_list_files_returns_records(self, db):
        from ouvrage.server.handlers.files_handler import _handle_list_files
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

    async def test_list_files_filter_by_task_id(self, db, sample_task):
        from ouvrage.server.handlers.files_handler import _handle_list_files
        task_id = sample_task["id"]
        fid1 = str(uuid.uuid4())
        fid2 = str(uuid.uuid4())
        await db.create_file(
            id=fid1, filename="task_file.txt", stored_path="/tmp/task_file.txt",
            mime_type="text/plain", size_bytes=100, uploaded_by=None, task_id=task_id,
        )
        await db.create_file(
            id=fid2, filename="global_file.txt", stored_path="/tmp/global_file.txt",
            mime_type="text/plain", size_bytes=200, uploaded_by=None, task_id=None,
        )
        result = await _handle_list_files({"task_id": task_id})
        assert len(result["files"]) == 1
        assert result["files"][0]["id"] == fid1

    async def test_list_files_no_filter_returns_all(self, db, sample_task):
        from ouvrage.server.handlers.files_handler import _handle_list_files
        fid1 = str(uuid.uuid4())
        fid2 = str(uuid.uuid4())
        await db.create_file(
            id=fid1, filename="task_file.txt", stored_path="/tmp/f1.txt",
            mime_type="text/plain", size_bytes=100, uploaded_by=None, task_id=sample_task["id"],
        )
        await db.create_file(
            id=fid2, filename="global_file.txt", stored_path="/tmp/f2.txt",
            mime_type="text/plain", size_bytes=200, uploaded_by=None, task_id=None,
        )
        result = await _handle_list_files({})
        assert len(result["files"]) == 2


# ── Task-level file attachment tests ──────────────────────────────────────


class TestUploadWithTaskId:

    async def test_upload_with_task_id_stores_association(self, db, sample_task, tmp_uploads):
        task_id = sample_task["id"]
        file_data = b"task reference content"
        body, boundary = _make_multipart_with_fields("ref.txt", file_data, {"task_id": task_id})
        scope = _upload_scope("ref.txt", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 201
        data = resp.json()
        assert data["task_id"] == task_id

    async def test_upload_with_task_id_stored_in_db(self, db, sample_task, tmp_uploads):
        task_id = sample_task["id"]
        file_data = b"hello"
        body, boundary = _make_multipart_with_fields("note.txt", file_data, {"task_id": task_id})
        scope = _upload_scope("note.txt", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 201
        file_id = resp.json()["id"]
        record = await db.get_file(file_id)
        assert record["task_id"] == task_id

    async def test_upload_with_invalid_task_id_returns_404(self, db, tmp_uploads):
        file_data = b"data"
        body, boundary = _make_multipart_with_fields("x.txt", file_data, {"task_id": "nonexistent/task"})
        scope = _upload_scope("x.txt", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 404

    async def test_upload_without_task_id_is_global(self, db, tmp_uploads):
        file_data = b"global file"
        body, boundary = _make_multipart("global.txt", file_data)
        scope = _upload_scope("global.txt", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 201
        assert resp.json()["task_id"] is None


class TestListFilesByTaskId:

    async def test_list_files_with_task_id_filter(self, db, sample_task):
        task_id = sample_task["id"]
        fid1 = str(uuid.uuid4())
        fid2 = str(uuid.uuid4())
        await db.create_file(
            id=fid1, filename="t.txt", stored_path="/tmp/t.txt",
            mime_type="text/plain", size_bytes=10, uploaded_by=None, task_id=task_id,
        )
        await db.create_file(
            id=fid2, filename="g.txt", stored_path="/tmp/g.txt",
            mime_type="text/plain", size_bytes=20, uploaded_by=None, task_id=None,
        )
        scope = _make_scope("/dashboard/api/files", "GET")
        scope["query_string"] = f"task_id={task_id}".encode()
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        files = resp.json()
        assert len(files) == 1
        assert files[0]["id"] == fid1

    async def test_list_files_without_filter_returns_all(self, db, sample_task):
        task_id = sample_task["id"]
        fid1 = str(uuid.uuid4())
        fid2 = str(uuid.uuid4())
        await db.create_file(
            id=fid1, filename="t.txt", stored_path="/tmp/t1.txt",
            mime_type="text/plain", size_bytes=10, uploaded_by=None, task_id=task_id,
        )
        await db.create_file(
            id=fid2, filename="g.txt", stored_path="/tmp/g1.txt",
            mime_type="text/plain", size_bytes=20, uploaded_by=None, task_id=None,
        )
        scope = _make_scope("/dashboard/api/files", "GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        assert len(resp.json()) == 2


class TestTaskFilesEndpoint:

    async def test_get_task_files_empty(self, db, sample_task):
        task_id = sample_task["id"]
        scope = _make_scope(f"/dashboard/api/tasks/{task_id}/files", "GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        assert resp.json() == []

    async def test_get_task_files_returns_task_files(self, db, sample_task):
        task_id = sample_task["id"]
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid, filename="spec.md", stored_path="/tmp/spec.md",
            mime_type="text/markdown", size_bytes=500, uploaded_by=None, task_id=task_id,
        )
        scope = _make_scope(f"/dashboard/api/tasks/{task_id}/files", "GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        files = resp.json()
        assert len(files) == 1
        assert files[0]["id"] == fid
        assert files[0]["task_id"] == task_id

    async def test_get_task_files_excludes_other_task_files(self, db, sample_task, sample_project):
        task_id = sample_task["id"]
        other_task = await db.create_task(
            id="test-project/other-task",
            project_id="test-project",
            goal="Another task",
        )
        fid_mine = str(uuid.uuid4())
        fid_other = str(uuid.uuid4())
        await db.create_file(
            id=fid_mine, filename="mine.txt", stored_path="/tmp/mine.txt",
            mime_type="text/plain", size_bytes=10, uploaded_by=None, task_id=task_id,
        )
        await db.create_file(
            id=fid_other, filename="other.txt", stored_path="/tmp/other.txt",
            mime_type="text/plain", size_bytes=10, uploaded_by=None, task_id=other_task["id"],
        )
        scope = _make_scope(f"/dashboard/api/tasks/{task_id}/files", "GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        ids = [f["id"] for f in resp.json()]
        assert fid_mine in ids
        assert fid_other not in ids

    async def test_get_task_files_nonexistent_task(self, db):
        scope = _make_scope("/dashboard/api/tasks/no-such-task/files", "GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 404


class TestPromptInjection:

    async def test_prompt_includes_reference_files(self, db, sample_task, sample_project):
        from ouvrage.dispatch.sdk_session import _build_task_prompt
        task_id = sample_task["id"]
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid, filename="design.md", stored_path="/data/uploads/abc/design.md",
            mime_type="text/markdown", size_bytes=2048, uploaded_by=None, task_id=task_id,
        )
        prompt = await _build_task_prompt(sample_project, sample_task, "Do the thing")
        assert "## Reference Files" in prompt
        assert "/data/uploads/abc/design.md" in prompt
        assert "text/markdown" in prompt
        assert "2.0KB" in prompt
        assert "Read these files when relevant" in prompt

    async def test_prompt_no_reference_files_section_when_none(self, db, sample_task, sample_project):
        from ouvrage.dispatch.sdk_session import _build_task_prompt
        prompt = await _build_task_prompt(sample_project, sample_task, "Do the thing")
        assert "## Reference Files" not in prompt

    async def test_prompt_excludes_global_files(self, db, sample_task, sample_project):
        from ouvrage.dispatch.sdk_session import _build_task_prompt
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid, filename="global.txt", stored_path="/data/uploads/xyz/global.txt",
            mime_type="text/plain", size_bytes=100, uploaded_by=None, task_id=None,
        )
        prompt = await _build_task_prompt(sample_project, sample_task, "Do the thing")
        # Global files should NOT appear in task prompt
        assert "## Reference Files" not in prompt

    async def test_prompt_multiple_files(self, db, sample_task, sample_project):
        from ouvrage.dispatch.sdk_session import _build_task_prompt
        task_id = sample_task["id"]
        for i, name in enumerate(["a.txt", "b.pdf"]):
            fid = str(uuid.uuid4())
            await db.create_file(
                id=fid, filename=name, stored_path=f"/data/uploads/{i}/{name}",
                mime_type="text/plain", size_bytes=1024 * (i + 1), uploaded_by=None, task_id=task_id,
            )
        prompt = await _build_task_prompt(sample_project, sample_task, "Do the thing")
        assert "a.txt" in prompt
        assert "b.pdf" in prompt

    async def test_prompt_includes_producing_files_section(self, db, sample_task, sample_project):
        from ouvrage.dispatch.sdk_session import _build_task_prompt
        prompt = await _build_task_prompt(sample_project, sample_task, "Do the thing")
        assert "## Producing Files" in prompt
        assert "add_task_file" in prompt


class TestReactiveInjection:

    async def test_working_task_gets_notification(self, db, sample_task, tmp_uploads):
        """Upload to a working task should post a task message."""
        import asyncio
        task_id = sample_task["id"]
        assert sample_task["status"] == "working"

        file_data = b"important reference"
        body, boundary = _make_multipart_with_fields("ref.txt", file_data, {"task_id": task_id})
        scope = _upload_scope("ref.txt", body, boundary)

        mock_post = AsyncMock(return_value={"id": 999})
        with patch("ouvrage.db.post_task_message", mock_post):
            resp = _Capture()
            await handle_request(scope, _make_receive(body), resp)
            # Give the background task a chance to run
            await asyncio.sleep(0)

        assert resp.status == 201
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        assert call_kwargs["task_id"] == task_id
        assert call_kwargs["author"] == "switchboard"
        assert call_kwargs["type"] == "note"
        assert "📎" in call_kwargs["content"]
        assert "ref.txt" in call_kwargs["content"]

    async def test_working_task_message_visible_in_thread(self, db, sample_task, tmp_uploads):
        """The reactive message actually appears in the task thread."""
        import asyncio
        task_id = sample_task["id"]
        file_data = b"data"
        body, boundary = _make_multipart_with_fields("doc.txt", file_data, {"task_id": task_id})
        scope = _upload_scope("doc.txt", body, boundary)

        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        # Give the background task enough time to complete (aiosqlite uses background threads)
        await asyncio.sleep(0.2)

        assert resp.status == 201
        # Check the message appears in the task thread
        thread = await db.read_task_messages(task_id)
        notes = [m for m in thread.get("messages", []) if m.get("type") == "note" and m.get("author") == "switchboard"]
        assert len(notes) == 1
        assert "📎" in notes[0]["content"]
        assert "doc.txt" in notes[0]["content"]

    async def test_non_working_task_no_notification(self, db, sample_project, tmp_uploads):
        """Upload to a non-working task should NOT post a task message."""
        ready_task = await db.create_task(
            id="test-project/ready-task",
            project_id="test-project",
            goal="Ready task",
        )
        assert ready_task["status"] == "ready"

        file_data = b"file"
        body, boundary = _make_multipart_with_fields("x.txt", file_data, {"task_id": ready_task["id"]})
        scope = _upload_scope("x.txt", body, boundary)

        mock_post = AsyncMock(return_value={"id": 1})
        with patch("ouvrage.db.post_task_message", mock_post):
            resp = _Capture()
            await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 201
        mock_post.assert_not_called()

    async def test_no_task_id_no_notification(self, db, tmp_uploads):
        """Upload without task_id should NOT post any task message."""
        file_data = b"file"
        body, boundary = _make_multipart("y.txt", file_data)
        scope = _upload_scope("y.txt", body, boundary)

        mock_post = AsyncMock(return_value={"id": 1})
        with patch("ouvrage.db.post_task_message", mock_post):
            resp = _Capture()
            await handle_request(scope, _make_receive(body), resp)

        assert resp.status == 201
        mock_post.assert_not_called()


# ── add_task_file MCP tool ─────────────────────────────────────────────────


class TestAddTaskFile:
    """Tests for the add_task_file worker MCP tool."""

    @pytest.fixture(autouse=True)
    def patch_uploads_dir(self, tmp_path, monkeypatch):
        """Redirect uploads to a temp directory."""
        uploads = tmp_path / "uploads"
        uploads.mkdir(exist_ok=True)
        monkeypatch.setattr(
            "ouvrage.server.handlers.files_handler._uploads_dir",
            lambda: uploads,
        )
        self._uploads = uploads

    @pytest.fixture(autouse=True)
    def patch_worker_context(self, monkeypatch):
        """Default: pretend we are on the worker endpoint."""
        monkeypatch.setattr(
            "ouvrage.server.handlers.files_handler.get_request_is_worker",
            lambda: True,
        )

    async def _make_worktree(self, tmp_path: Path) -> Path:
        wt = tmp_path / "worktree"
        wt.mkdir()
        return wt

    async def test_successful_copy(self, db, sample_task, tmp_path):
        """File is copied to uploads dir and DB record is created."""
        from ouvrage.server.handlers.files_handler import _handle_add_task_file

        wt = await self._make_worktree(tmp_path)
        src = wt / "report.txt"
        src.write_text("hello output")

        await db.update_task(sample_task["id"], worktree_path=str(wt))

        result = await _handle_add_task_file({
            "task_id": sample_task["id"],
            "source_path": str(src),
        })

        assert result["filename"] == "report.txt"
        assert result["size_bytes"] == len(b"hello output")
        assert Path(result["stored_path"]).exists()
        assert Path(result["stored_path"]).read_text() == "hello output"

        record = await db.get_file(result["id"])
        assert record is not None
        assert record["task_id"] == sample_task["id"]
        assert record["uploaded_by"] is None
        assert record["filename"] == "report.txt"

    async def test_custom_filename(self, db, sample_task, tmp_path):
        """Custom filename parameter is used as display name."""
        from ouvrage.server.handlers.files_handler import _handle_add_task_file

        wt = await self._make_worktree(tmp_path)
        src = wt / "output.json"
        src.write_text("{}")
        await db.update_task(sample_task["id"], worktree_path=str(wt))

        result = await _handle_add_task_file({
            "task_id": sample_task["id"],
            "source_path": str(src),
            "filename": "analysis-results.json",
        })

        assert result["filename"] == "analysis-results.json"
        assert Path(result["stored_path"]).name == "analysis-results.json"

    async def test_path_traversal_rejected(self, db, sample_task, tmp_path):
        """Source path outside worktree is rejected."""
        from ouvrage.server.handlers.files_handler import _handle_add_task_file

        wt = await self._make_worktree(tmp_path)
        await db.update_task(sample_task["id"], worktree_path=str(wt))

        outside = tmp_path / "secret.txt"
        outside.write_text("sensitive")

        with pytest.raises(ValueError, match="within the worktree"):
            await _handle_add_task_file({
                "task_id": sample_task["id"],
                "source_path": str(outside),
            })

    async def test_bad_extension_rejected(self, db, sample_task, tmp_path):
        """Files with disallowed extensions are rejected."""
        from ouvrage.server.handlers.files_handler import _handle_add_task_file

        wt = await self._make_worktree(tmp_path)
        await db.update_task(sample_task["id"], worktree_path=str(wt))

        src = wt / "script.sh"
        src.write_text("#!/bin/bash")

        with pytest.raises(ValueError, match="not allowed"):
            await _handle_add_task_file({
                "task_id": sample_task["id"],
                "source_path": str(src),
            })

    async def test_size_limit_rejected(self, db, sample_task, tmp_path):
        """Files exceeding 10MB are rejected."""
        from ouvrage.server.handlers.files_handler import _handle_add_task_file

        wt = await self._make_worktree(tmp_path)
        await db.update_task(sample_task["id"], worktree_path=str(wt))

        src = wt / "big.txt"
        src.write_bytes(b"x" * (10 * 1024 * 1024 + 1))

        with pytest.raises(ValueError, match="10MB"):
            await _handle_add_task_file({
                "task_id": sample_task["id"],
                "source_path": str(src),
            })

    async def test_file_not_found(self, db, sample_task, tmp_path):
        """Non-existent source path raises an error."""
        from ouvrage.server.handlers.files_handler import _handle_add_task_file

        wt = await self._make_worktree(tmp_path)
        await db.update_task(sample_task["id"], worktree_path=str(wt))

        with pytest.raises(ValueError, match="File not found"):
            await _handle_add_task_file({
                "task_id": sample_task["id"],
                "source_path": str(wt / "nonexistent.txt"),
            })

    async def test_worker_only_enforced(self, db, sample_task, tmp_path, monkeypatch):
        """Tool raises ValueError when called from a non-worker context."""
        from ouvrage.server.handlers.files_handler import _handle_add_task_file

        monkeypatch.setattr(
            "ouvrage.server.handlers.files_handler.get_request_is_worker",
            lambda: False,
        )

        wt = await self._make_worktree(tmp_path)
        await db.update_task(sample_task["id"], worktree_path=str(wt))
        src = wt / "file.txt"
        src.write_text("content")

        with pytest.raises(ValueError, match="worker endpoint"):
            await _handle_add_task_file({
                "task_id": sample_task["id"],
                "source_path": str(src),
            })

    async def test_uploaded_by_is_null(self, db, sample_task, tmp_path):
        """Files added via add_task_file have uploaded_by=None."""
        from ouvrage.server.handlers.files_handler import _handle_add_task_file

        wt = await self._make_worktree(tmp_path)
        src = wt / "output.md"
        src.write_text("# Report")
        await db.update_task(sample_task["id"], worktree_path=str(wt))

        result = await _handle_add_task_file({
            "task_id": sample_task["id"],
            "source_path": str(src),
        })

        record = await db.get_file(result["id"])
        assert record["uploaded_by"] is None


# ── Schema: project_id column ──────────────────────────────────────────────


class TestSchemaProjectId:
    """Verify the project_id column is added to the files table via migration."""

    async def test_project_id_column_exists(self, db):
        import ouvrage.db.connection as _conn
        async with _conn.get_db() as conn:
            cols = await conn.execute_fetchall("PRAGMA table_info(files)")
        col_names = [c["name"] for c in cols]
        assert "project_id" in col_names

    async def test_create_file_with_project_id(self, db, sample_project):
        fid = str(uuid.uuid4())
        record = await db.create_file(
            id=fid,
            filename="spec.md",
            stored_path="/data/uploads/spec.md",
            mime_type="text/markdown",
            size_bytes=100,
            uploaded_by=None,
            project_id=sample_project["id"],
        )
        assert record["project_id"] == sample_project["id"]
        assert record["task_id"] is None

    async def test_create_file_with_both_ids(self, db, sample_project, sample_task):
        """A promoted file can have both task_id and project_id."""
        fid = str(uuid.uuid4())
        record = await db.create_file(
            id=fid,
            filename="artifact.txt",
            stored_path="/data/uploads/artifact.txt",
            mime_type="text/plain",
            size_bytes=50,
            uploaded_by=None,
            task_id=sample_task["id"],
            project_id=sample_project["id"],
        )
        assert record["task_id"] == sample_task["id"]
        assert record["project_id"] == sample_project["id"]

    async def test_get_file_returns_project_id(self, db, sample_project):
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid,
            filename="doc.txt",
            stored_path="/data/uploads/doc.txt",
            mime_type="text/plain",
            size_bytes=10,
            uploaded_by=None,
            project_id=sample_project["id"],
        )
        record = await db.get_file(fid)
        assert record is not None
        assert record["project_id"] == sample_project["id"]


# ── list_files with project_id filter ─────────────────────────────────────


class TestListFilesProjectId:

    async def test_list_files_filter_by_project_id(self, db, sample_project, sample_task):
        """list_files(project_id=X) returns only files with that project_id."""
        project_id = sample_project["id"]
        fid_proj = str(uuid.uuid4())
        fid_task = str(uuid.uuid4())
        await db.create_file(
            id=fid_proj, filename="project_file.md", stored_path="/tmp/pf.md",
            mime_type="text/markdown", size_bytes=100, uploaded_by=None, project_id=project_id,
        )
        await db.create_file(
            id=fid_task, filename="task_file.txt", stored_path="/tmp/tf.txt",
            mime_type="text/plain", size_bytes=50, uploaded_by=None, task_id=sample_task["id"],
        )
        files = await db.list_files(project_id=project_id)
        assert len(files) == 1
        assert files[0]["id"] == fid_proj

    async def test_promoted_file_visible_under_project(self, db, sample_project, sample_task):
        """A promoted file (both task_id and project_id set) appears under project listing."""
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid, filename="promoted.txt", stored_path="/tmp/promo.txt",
            mime_type="text/plain", size_bytes=100, uploaded_by=None,
            task_id=sample_task["id"], project_id=sample_project["id"],
        )
        proj_files = await db.list_files(project_id=sample_project["id"])
        task_files = await db.list_files(task_id=sample_task["id"])
        proj_ids = [f["id"] for f in proj_files]
        task_ids = [f["id"] for f in task_files]
        assert fid in proj_ids
        assert fid in task_ids

    async def test_mcp_list_files_filter_by_project_id(self, db, sample_project):
        """MCP list_files handler filters by project_id when provided."""
        from ouvrage.server.handlers.files_handler import _handle_list_files
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid, filename="ref.md", stored_path="/tmp/ref.md",
            mime_type="text/markdown", size_bytes=200, uploaded_by=None,
            project_id=sample_project["id"],
        )
        result = await _handle_list_files({"project_id": sample_project["id"]})
        assert len(result["files"]) == 1
        assert result["files"][0]["id"] == fid
        assert result["files"][0]["readable"] is True

    async def test_dashboard_list_files_filter_by_project_id(self, db, sample_project):
        """Dashboard GET /dashboard/api/files?project_id=X returns project files."""
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid, filename="arch.md", stored_path="/tmp/arch.md",
            mime_type="text/markdown", size_bytes=300, uploaded_by=None,
            project_id=sample_project["id"],
        )
        scope = _make_scope("/dashboard/api/files", "GET")
        scope["query_string"] = f"project_id={sample_project['id']}".encode()
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        files = resp.json()
        assert len(files) == 1
        assert files[0]["id"] == fid


# ── GET /dashboard/api/files/{id} ─────────────────────────────────────────


class TestGetFileEndpoint:

    async def _insert_text_file(self, tmp_uploads, content: bytes = b"text content") -> tuple[str, Path]:
        fid = str(uuid.uuid4())
        uuid_dir = tmp_uploads / fid
        uuid_dir.mkdir()
        dest = uuid_dir / "notes.txt"
        dest.write_bytes(content)
        await db.create_file(
            id=fid,
            filename="notes.txt",
            stored_path=str(dest),
            mime_type="text/plain",
            size_bytes=len(content),
            uploaded_by=1,
        )
        return fid, dest

    async def _insert_binary_file(self, tmp_uploads) -> str:
        fid = str(uuid.uuid4())
        uuid_dir = tmp_uploads / fid
        uuid_dir.mkdir()
        dest = uuid_dir / "image.png"
        dest.write_bytes(b"\x89PNG" + b"\x00" * 100)
        await db.create_file(
            id=fid,
            filename="image.png",
            stored_path=str(dest),
            mime_type="image/png",
            size_bytes=104,
            uploaded_by=1,
        )
        return fid

    async def test_get_text_file_returns_metadata(self, db, tmp_uploads):
        fid, _ = await self._insert_text_file(tmp_uploads)
        scope = _make_scope(f"/dashboard/api/files/{fid}", "GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        data = resp.json()
        assert data["id"] == fid
        assert data["filename"] == "notes.txt"
        assert data["mime_type"] == "text/plain"
        assert data["readable"] is True

    async def test_get_binary_file_returns_metadata(self, db, tmp_uploads):
        fid = await self._insert_binary_file(tmp_uploads)
        scope = _make_scope(f"/dashboard/api/files/{fid}", "GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        data = resp.json()
        assert data["id"] == fid
        assert data["readable"] is False

    async def test_get_file_not_found(self, db, tmp_uploads):
        scope = _make_scope("/dashboard/api/files/nonexistent-uuid", "GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 404

    async def test_get_file_requires_auth(self, db, tmp_uploads):
        fid, _ = await self._insert_text_file(tmp_uploads)
        scope = {
            "type": "http",
            "method": "GET",
            "path": f"/dashboard/api/files/{fid}",
            "query_string": b"",
            "headers": [],
        }
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 401

    async def test_get_file_with_project_id(self, db, sample_project, tmp_uploads):
        fid = str(uuid.uuid4())
        uuid_dir = tmp_uploads / fid
        uuid_dir.mkdir()
        dest = uuid_dir / "plan.md"
        dest.write_bytes(b"## Plan")
        await db.create_file(
            id=fid, filename="plan.md", stored_path=str(dest),
            mime_type="text/markdown", size_bytes=7, uploaded_by=1,
            project_id=sample_project["id"],
        )
        scope = _make_scope(f"/dashboard/api/files/{fid}", "GET")
        resp = _Capture()
        await handle_request(scope, _make_receive(), resp)
        assert resp.status == 200
        data = resp.json()
        assert data["project_id"] == sample_project["id"]


# ── get_file MCP tool ──────────────────────────────────────────────────────


class TestGetFileTool:

    async def test_get_readable_file_returns_content(self, db, tmp_path):
        from ouvrage.server.handlers.files_handler import _handle_get_file
        fid = str(uuid.uuid4())
        dest = tmp_path / "data.txt"
        dest.write_bytes(b"hello world")
        await db.create_file(
            id=fid, filename="data.txt", stored_path=str(dest),
            mime_type="text/plain", size_bytes=11, uploaded_by=None,
        )
        result = await _handle_get_file({"id": fid})
        assert result["id"] == fid
        assert result["content"] == "hello world"
        assert result["readable"] is True
        assert result["truncated"] is False

    async def test_get_binary_file_returns_metadata_only(self, db, tmp_path):
        from ouvrage.server.handlers.files_handler import _handle_get_file
        fid = str(uuid.uuid4())
        dest = tmp_path / "photo.png"
        dest.write_bytes(b"\x89PNG" + b"\x00" * 50)
        await db.create_file(
            id=fid, filename="photo.png", stored_path=str(dest),
            mime_type="image/png", size_bytes=54, uploaded_by=None,
        )
        result = await _handle_get_file({"id": fid})
        assert result["id"] == fid
        assert result["readable"] is False
        assert "content" not in result

    async def test_get_file_not_found_raises(self, db):
        from ouvrage.server.handlers.files_handler import _handle_get_file
        with pytest.raises(ValueError, match="not found"):
            await _handle_get_file({"id": "nonexistent-uuid"})

    async def test_get_file_truncates_large_content(self, db, tmp_path):
        from ouvrage.server.handlers.files_handler import _handle_get_file
        fid = str(uuid.uuid4())
        dest = tmp_path / "big.txt"
        dest.write_bytes(b"x" * 2000)
        await db.create_file(
            id=fid, filename="big.txt", stored_path=str(dest),
            mime_type="text/plain", size_bytes=2000, uploaded_by=None,
        )
        result = await _handle_get_file({"id": fid, "max_bytes": 100})
        assert result["truncated"] is True
        assert len(result["content"]) == 100

    async def test_get_file_returns_project_id(self, db, sample_project, tmp_path):
        from ouvrage.server.handlers.files_handler import _handle_get_file
        fid = str(uuid.uuid4())
        dest = tmp_path / "ref.txt"
        dest.write_bytes(b"reference content")
        await db.create_file(
            id=fid, filename="ref.txt", stored_path=str(dest),
            mime_type="text/plain", size_bytes=17, uploaded_by=None,
            project_id=sample_project["id"],
        )
        result = await _handle_get_file({"id": fid})
        assert result["project_id"] == sample_project["id"]
        assert result["task_id"] is None

    async def test_get_file_missing_id_raises(self, db):
        from ouvrage.server.handlers.files_handler import _handle_get_file
        with pytest.raises(ValueError, match="file_id is required"):
            await _handle_get_file({})

    async def test_get_file_accepts_file_id_param(self, db, tmp_path):
        """get_file accepts 'file_id' param (for get_attached_file alias compat)."""
        from ouvrage.server.handlers.files_handler import _handle_get_file
        fid = str(uuid.uuid4())
        dest = tmp_path / "compat.txt"
        dest.write_bytes(b"compat content")
        await db.create_file(
            id=fid, filename="compat.txt", stored_path=str(dest),
            mime_type="text/plain", size_bytes=14, uploaded_by=None,
        )
        result = await _handle_get_file({"file_id": fid})
        assert result["id"] == fid
        assert result["content"] == "compat content"

    async def test_get_file_same_result_as_get_attached_file(self, db, tmp_path):
        """get_file and get_attached_file (deprecated alias) return equivalent content."""
        from ouvrage.server.handlers.files_handler import _handle_get_file
        fid = str(uuid.uuid4())
        dest = tmp_path / "shared.txt"
        dest.write_bytes(b"shared file content")
        await db.create_file(
            id=fid, filename="shared.txt", stored_path=str(dest),
            mime_type="text/plain", size_bytes=19, uploaded_by=None,
        )
        # get_file via "id" param
        result_new = await _handle_get_file({"id": fid})
        # get_attached_file via "file_id" param (same handler after alias redirect)
        result_deprecated = await _handle_get_file({"file_id": fid})
        assert result_new["content"] == result_deprecated["content"]
        assert result_new["id"] == result_deprecated["id"]


# ── add_project_file MCP tool ──────────────────────────────────────────────


class TestAddProjectFile:
    """Tests for the add_project_file worker MCP tool."""

    @pytest.fixture(autouse=True)
    def patch_uploads_dir(self, tmp_path, monkeypatch):
        uploads = tmp_path / "uploads"
        uploads.mkdir(exist_ok=True)
        monkeypatch.setattr(
            "ouvrage.server.handlers.files_handler._uploads_dir",
            lambda: uploads,
        )
        self._uploads = uploads

    @pytest.fixture(autouse=True)
    def patch_worker_context(self, monkeypatch):
        monkeypatch.setattr(
            "ouvrage.server.handlers.files_handler.get_request_is_worker",
            lambda: True,
        )

    @pytest.fixture
    async def worker_task(self, db, sample_project, tmp_path):
        """A task with worktree_path set to tmp_path for upload validation."""
        task = await db.create_task(
            id="test-project/file-upload-task",
            project_id="test-project",
            goal="File upload test",
        )
        await db.update_task(task["id"], worktree_path=str(tmp_path))
        return task

    async def test_successful_add_project_file(self, db, sample_project, worker_task, tmp_path):
        from ouvrage.server.handlers.files_handler import _handle_add_project_file
        src = tmp_path / "readme.md"
        src.write_bytes(b"# Project README")

        result = await _handle_add_project_file({
            "project_id": sample_project["id"],
            "task_id": worker_task["id"],
            "source_path": str(src),
        })

        assert result["filename"] == "readme.md"
        assert result["project_id"] == sample_project["id"]
        assert Path(result["stored_path"]).exists()

        record = await db.get_file(result["id"])
        assert record is not None
        assert record["project_id"] == sample_project["id"]
        assert record["task_id"] is None
        assert record["uploaded_by"] is None

    async def test_custom_filename(self, db, sample_project, worker_task, tmp_path):
        from ouvrage.server.handlers.files_handler import _handle_add_project_file
        src = tmp_path / "output.json"
        src.write_bytes(b"{}")

        result = await _handle_add_project_file({
            "project_id": sample_project["id"],
            "task_id": worker_task["id"],
            "source_path": str(src),
            "filename": "project-data.json",
        })

        assert result["filename"] == "project-data.json"
        assert Path(result["stored_path"]).name == "project-data.json"

    async def test_project_not_found_raises(self, db, sample_project, worker_task, tmp_path):
        from ouvrage.server.handlers.files_handler import _handle_add_project_file
        src = tmp_path / "test.txt"
        src.write_bytes(b"data")

        with pytest.raises(ValueError, match="not found"):
            await _handle_add_project_file({
                "project_id": "nonexistent-project",
                "task_id": worker_task["id"],
                "source_path": str(src),
            })

    async def test_bad_extension_rejected(self, db, sample_project, worker_task, tmp_path):
        from ouvrage.server.handlers.files_handler import _handle_add_project_file
        src = tmp_path / "script.sh"
        src.write_bytes(b"#!/bin/bash")

        with pytest.raises(ValueError, match="not allowed"):
            await _handle_add_project_file({
                "project_id": sample_project["id"],
                "task_id": worker_task["id"],
                "source_path": str(src),
            })

    async def test_worker_only_enforced(self, db, sample_project, tmp_path, monkeypatch):
        from ouvrage.server.handlers.files_handler import _handle_add_project_file
        monkeypatch.setattr(
            "ouvrage.server.handlers.files_handler.get_request_is_worker",
            lambda: False,
        )
        src = tmp_path / "doc.txt"
        src.write_bytes(b"content")

        with pytest.raises(ValueError, match="worker endpoint"):
            await _handle_add_project_file({
                "project_id": sample_project["id"],
                "task_id": "test-project/some-task",
                "source_path": str(src),
            })

    async def test_missing_project_id_raises(self, db, tmp_path):
        from ouvrage.server.handlers.files_handler import _handle_add_project_file
        src = tmp_path / "x.txt"
        src.write_bytes(b"x")

        with pytest.raises(ValueError, match="project_id is required"):
            await _handle_add_project_file({"source_path": str(src)})

    async def test_missing_source_path_raises(self, db, sample_project):
        from ouvrage.server.handlers.files_handler import _handle_add_project_file

        with pytest.raises(ValueError, match="source_path is required"):
            await _handle_add_project_file({"project_id": sample_project["id"]})


# ── promote_task_file ──────────────────────────────────────────────────────


class TestPromoteTaskFile:
    """Tests for promote_task_file — MCP tool and dashboard endpoint."""

    @pytest.fixture(autouse=True)
    def patch_uploads_dir(self, tmp_path, monkeypatch):
        uploads = tmp_path / "uploads"
        uploads.mkdir(exist_ok=True)
        monkeypatch.setattr(
            "ouvrage.server.handlers.files_handler._uploads_dir",
            lambda: uploads,
        )

    async def _insert_task_file(self, db, sample_task) -> str:
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid, filename="result.txt", stored_path=f"/tmp/{fid}/result.txt",
            mime_type="text/plain", size_bytes=100, uploaded_by=None,
            task_id=sample_task["id"],
        )
        return fid

    async def test_db_promote_task_file(self, db, sample_project, sample_task):
        """db.promote_task_file sets project_id on a task file."""
        fid = await self._insert_task_file(db, sample_task)
        record = await db.promote_task_file(fid, sample_project["id"])
        assert record is not None
        assert record["project_id"] == sample_project["id"]
        assert record["task_id"] == sample_task["id"]

    async def test_db_promote_returns_none_for_no_task_id(self, db, sample_project):
        """db.promote_task_file returns None if file has no task_id."""
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid, filename="global.txt", stored_path="/tmp/global.txt",
            mime_type="text/plain", size_bytes=50, uploaded_by=None,
        )
        result = await db.promote_task_file(fid, sample_project["id"])
        assert result is None

    async def test_db_promote_nonexistent_file(self, db, sample_project):
        """db.promote_task_file returns None for a nonexistent file ID."""
        result = await db.promote_task_file("nonexistent-uuid", sample_project["id"])
        assert result is None

    async def test_mcp_promote_task_file(self, db, sample_project, sample_task):
        """MCP promote_task_file handler promotes a file successfully."""
        from ouvrage.server.handlers.files_handler import _handle_promote_task_file
        fid = await self._insert_task_file(db, sample_task)

        result = await _handle_promote_task_file({
            "file_id": fid,
            "project_id": sample_project["id"],
        })

        assert result["project_id"] == sample_project["id"]
        assert result["task_id"] == sample_task["id"]

    async def test_mcp_promote_invalid_project(self, db, sample_task):
        """MCP promote_task_file raises if project does not exist."""
        from ouvrage.server.handlers.files_handler import _handle_promote_task_file
        fid = await self._insert_task_file(db, sample_task)

        with pytest.raises(ValueError, match="not found"):
            await _handle_promote_task_file({
                "file_id": fid,
                "project_id": "nonexistent-project",
            })

    async def test_mcp_promote_non_task_file_raises(self, db, sample_project):
        """MCP promote_task_file raises if file has no task_id."""
        from ouvrage.server.handlers.files_handler import _handle_promote_task_file
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid, filename="global.txt", stored_path="/tmp/global.txt",
            mime_type="text/plain", size_bytes=50, uploaded_by=None,
        )

        with pytest.raises(ValueError, match="only task files can be promoted"):
            await _handle_promote_task_file({
                "file_id": fid,
                "project_id": sample_project["id"],
            })

    async def test_mcp_promote_missing_args(self, db, sample_project, sample_task):
        """Missing required args raise ValueError."""
        from ouvrage.server.handlers.files_handler import _handle_promote_task_file
        fid = await self._insert_task_file(db, sample_task)

        with pytest.raises(ValueError, match="file_id is required"):
            await _handle_promote_task_file({"project_id": sample_project["id"]})

        with pytest.raises(ValueError, match="project_id is required"):
            await _handle_promote_task_file({"file_id": fid})

    async def test_dashboard_promote_endpoint(self, db, sample_project, sample_task):
        """POST /dashboard/api/files/{id}/promote sets project_id."""
        fid = await self._insert_task_file(db, sample_task)
        body = json.dumps({"project_id": sample_project["id"]}).encode()
        scope = _make_scope(f"/dashboard/api/files/{fid}/promote", "POST")
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 200
        data = resp.json()
        assert data["project_id"] == sample_project["id"]
        assert data["task_id"] == sample_task["id"]

    async def test_dashboard_promote_requires_auth(self, db, sample_project, sample_task):
        """Dashboard promote endpoint rejects unauthenticated requests."""
        fid = await self._insert_task_file(db, sample_task)
        body = json.dumps({"project_id": sample_project["id"]}).encode()
        scope = {
            "type": "http",
            "method": "POST",
            "path": f"/dashboard/api/files/{fid}/promote",
            "query_string": b"",
            "headers": [],
        }
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 401

    async def test_dashboard_promote_invalid_project(self, db, sample_task):
        """Dashboard promote returns 404 for nonexistent project."""
        fid = await self._insert_task_file(db, sample_task)
        body = json.dumps({"project_id": "no-such-project"}).encode()
        scope = _make_scope(f"/dashboard/api/files/{fid}/promote", "POST")
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 404

    async def test_dashboard_promote_non_task_file(self, db, sample_project):
        """Dashboard promote returns 400 for a file with no task_id."""
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid, filename="global.txt", stored_path="/tmp/global.txt",
            mime_type="text/plain", size_bytes=50, uploaded_by=None,
        )
        body = json.dumps({"project_id": sample_project["id"]}).encode()
        scope = _make_scope(f"/dashboard/api/files/{fid}/promote", "POST")
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 400

    async def test_dashboard_promote_file_appears_in_project_listing(
        self, db, sample_project, sample_task
    ):
        """After promotion, file appears in project file listing."""
        fid = await self._insert_task_file(db, sample_task)
        body = json.dumps({"project_id": sample_project["id"]}).encode()
        scope = _make_scope(f"/dashboard/api/files/{fid}/promote", "POST")
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 200

        # Verify file appears in project listing
        list_scope = _make_scope("/dashboard/api/files", "GET")
        list_scope["query_string"] = f"project_id={sample_project['id']}".encode()
        list_resp = _Capture()
        await handle_request(list_scope, _make_receive(), list_resp)
        assert list_resp.status == 200
        project_files = list_resp.json()
        assert any(f["id"] == fid for f in project_files)

    async def test_promote_task_file_works_from_user_endpoint(self, db, sample_project, sample_task):
        """promote_task_file handler works without worker context (user endpoint)."""
        from ouvrage.server.handlers.files_handler import _handle_promote_task_file
        fid = await self._insert_task_file(db, sample_task)
        # No is_worker check in promote_task_file — must work from user endpoint
        result = await _handle_promote_task_file({
            "file_id": fid,
            "project_id": sample_project["id"],
        })
        assert result["project_id"] == sample_project["id"]
        assert result["task_id"] == sample_task["id"]
        assert result["id"] == fid

    async def test_promote_task_file_in_user_tools_list(self):
        """promote_task_file is registered in TOOLS (user-facing endpoint)."""
        from ouvrage.server.tools import TOOLS
        names = {t.name for t in TOOLS}
        assert "promote_task_file" in names


# ── get_task_status files array ─────────────────────────────────────────────


class TestGetTaskStatusFilesArray:
    """get_task_status must include a files array in both slim and detail responses."""

    async def test_slim_response_includes_files(self, db, sample_task):
        """Slim (default) response includes files array."""
        from ouvrage.server.handlers.tasks import _handle_get_task_status
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid,
            filename="report.md",
            stored_path="/tmp/fake/report.md",
            mime_type="text/markdown",
            size_bytes=34000,
            uploaded_by=None,
            task_id=sample_task["id"],
        )
        result = await _handle_get_task_status({"task_id": sample_task["id"]})
        assert "files" in result
        assert len(result["files"]) == 1
        f = result["files"][0]
        assert f["id"] == fid
        assert f["filename"] == "report.md"
        assert f["size_bytes"] == 34000
        assert f["mime_type"] == "text/markdown"
        assert f["readable"] is True

    async def test_detail_response_includes_files(self, db, sample_task):
        """Detail response (include_detail=True) also includes files array."""
        from ouvrage.server.handlers.tasks import _handle_get_task_status
        fid = str(uuid.uuid4())
        await db.create_file(
            id=fid,
            filename="screenshot.png",
            stored_path="/tmp/fake/screenshot.png",
            mime_type="image/png",
            size_bytes=12000,
            uploaded_by=None,
            task_id=sample_task["id"],
        )
        result = await _handle_get_task_status({
            "task_id": sample_task["id"],
            "include_detail": True,
        })
        assert "files" in result
        assert len(result["files"]) == 1
        f = result["files"][0]
        assert f["id"] == fid
        assert f["readable"] is False  # png is not readable

    async def test_empty_files_array_when_no_files(self, db, sample_task):
        """Files array is empty (not missing) when task has no files."""
        from ouvrage.server.handlers.tasks import _handle_get_task_status
        result = await _handle_get_task_status({"task_id": sample_task["id"]})
        assert "files" in result
        assert result["files"] == []
