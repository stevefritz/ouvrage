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

    monkeypatch.setattr("switchboard.config.settings.UPLOADS_DIR", str(uploads))
    yield uploads
    # Cleanup is automatic via tmp_path


# ── GET /dashboard/api/files ───────────────────────────────────────────────


class TestListFiles:


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


# ── POST /dashboard/api/files ─────────────────────────────────────────────


class TestUploadFile:


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


# ── MCP list_files tool ────────────────────────────────────────────────────


class TestListFilesTool:

    async def test_list_files_empty(self, db):
        from switchboard.server.handlers.files_handler import _handle_list_files
        result = await _handle_list_files({})
        assert result == {"files": []}


# ── Task-level file attachment tests ──────────────────────────────────────


class TestUploadWithTaskId:


    async def test_upload_with_invalid_task_id_returns_404(self, db, tmp_uploads):
        file_data = b"data"
        body, boundary = _make_multipart_with_fields("x.txt", file_data, {"task_id": "nonexistent/task"})
        scope = _upload_scope("x.txt", body, boundary)
        resp = _Capture()
        await handle_request(scope, _make_receive(body), resp)
        assert resp.status == 404


class TestTaskFilesEndpoint:


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
        from switchboard.dispatch.sdk_session import _build_task_prompt
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


class TestReactiveInjection:


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


# ── add_task_file MCP tool ─────────────────────────────────────────────────


class TestAddTaskFile:
    """Tests for the add_task_file worker MCP tool."""

    @pytest.fixture(autouse=True)
    def patch_uploads_dir(self, tmp_path, monkeypatch):
        """Redirect uploads to a temp directory."""
        uploads = tmp_path / "uploads"
        uploads.mkdir(exist_ok=True)
        monkeypatch.setattr(
            "switchboard.server.handlers.files_handler._uploads_dir",
            lambda: uploads,
        )
        self._uploads = uploads

    @pytest.fixture(autouse=True)
    def patch_worker_context(self, monkeypatch):
        """Default: pretend we are on the worker endpoint."""
        monkeypatch.setattr(
            "switchboard.server.handlers.files_handler.get_request_is_worker",
            lambda: True,
        )

    async def _make_worktree(self, tmp_path: Path) -> Path:
        wt = tmp_path / "worktree"
        wt.mkdir()
        return wt

    async def test_successful_copy(self, db, sample_task, tmp_path):
        """File is copied to uploads dir and DB record is created."""
        from switchboard.server.handlers.files_handler import _handle_add_task_file

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


    async def test_path_traversal_rejected(self, db, sample_task, tmp_path):
        """Source path outside worktree is rejected."""
        from switchboard.server.handlers.files_handler import _handle_add_task_file

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
        from switchboard.server.handlers.files_handler import _handle_add_task_file

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
        from switchboard.server.handlers.files_handler import _handle_add_task_file

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
        from switchboard.server.handlers.files_handler import _handle_add_task_file

        wt = await self._make_worktree(tmp_path)
        await db.update_task(sample_task["id"], worktree_path=str(wt))

        with pytest.raises(ValueError, match="File not found"):
            await _handle_add_task_file({
                "task_id": sample_task["id"],
                "source_path": str(wt / "nonexistent.txt"),
            })

    async def test_worker_only_enforced(self, db, sample_task, tmp_path, monkeypatch):
        """Tool raises ValueError when called from a non-worker context."""
        from switchboard.server.handlers.files_handler import _handle_add_task_file

        monkeypatch.setattr(
            "switchboard.server.handlers.files_handler.get_request_is_worker",
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


# ── Schema: project_id column ──────────────────────────────────────────────


# ── list_files with project_id filter ─────────────────────────────────────


class TestListFilesProjectId:


    async def test_mcp_list_files_filter_by_project_id(self, db, sample_project):
        """MCP list_files handler filters by project_id when provided."""
        from switchboard.server.handlers.files_handler import _handle_list_files
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


# ── get_file MCP tool ──────────────────────────────────────────────────────


class TestGetFileTool:


    async def test_get_binary_file_returns_metadata_only(self, db, tmp_path):
        from switchboard.server.handlers.files_handler import _handle_get_file
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
        from switchboard.server.handlers.files_handler import _handle_get_file
        with pytest.raises(ValueError, match="not found"):
            await _handle_get_file({"id": "nonexistent-uuid"})


    async def test_get_file_missing_id_raises(self, db):
        from switchboard.server.handlers.files_handler import _handle_get_file
        with pytest.raises(ValueError, match="file_id is required"):
            await _handle_get_file({})

    async def test_get_file_accepts_file_id_param(self, db, tmp_path):
        """get_file accepts 'file_id' param (for get_attached_file alias compat)."""
        from switchboard.server.handlers.files_handler import _handle_get_file
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


# ── add_project_file MCP tool ──────────────────────────────────────────────


class TestAddProjectFile:
    """Tests for the add_project_file worker MCP tool."""

    @pytest.fixture(autouse=True)
    def patch_uploads_dir(self, tmp_path, monkeypatch):
        uploads = tmp_path / "uploads"
        uploads.mkdir(exist_ok=True)
        monkeypatch.setattr(
            "switchboard.server.handlers.files_handler._uploads_dir",
            lambda: uploads,
        )
        self._uploads = uploads

    @pytest.fixture(autouse=True)
    def patch_worker_context(self, monkeypatch):
        monkeypatch.setattr(
            "switchboard.server.handlers.files_handler.get_request_is_worker",
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


    async def test_project_not_found_raises(self, db, sample_project, worker_task, tmp_path):
        from switchboard.server.handlers.files_handler import _handle_add_project_file
        src = tmp_path / "test.txt"
        src.write_bytes(b"data")

        with pytest.raises(ValueError, match="not found"):
            await _handle_add_project_file({
                "project_id": "nonexistent-project",
                "task_id": worker_task["id"],
                "source_path": str(src),
            })

    async def test_bad_extension_rejected(self, db, sample_project, worker_task, tmp_path):
        from switchboard.server.handlers.files_handler import _handle_add_project_file
        src = tmp_path / "script.sh"
        src.write_bytes(b"#!/bin/bash")

        with pytest.raises(ValueError, match="not allowed"):
            await _handle_add_project_file({
                "project_id": sample_project["id"],
                "task_id": worker_task["id"],
                "source_path": str(src),
            })


    async def test_missing_project_id_raises(self, db, tmp_path):
        from switchboard.server.handlers.files_handler import _handle_add_project_file
        src = tmp_path / "x.txt"
        src.write_bytes(b"x")

        with pytest.raises(ValueError, match="project_id is required"):
            await _handle_add_project_file({"source_path": str(src)})

    async def test_missing_source_path_raises(self, db, sample_project):
        from switchboard.server.handlers.files_handler import _handle_add_project_file

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
            "switchboard.server.handlers.files_handler._uploads_dir",
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


    async def test_mcp_promote_task_file(self, db, sample_project, sample_task):
        """MCP promote_task_file handler promotes a file successfully."""
        from switchboard.server.handlers.files_handler import _handle_promote_task_file
        fid = await self._insert_task_file(db, sample_task)

        result = await _handle_promote_task_file({
            "file_id": fid,
            "project_id": sample_project["id"],
        })

        assert result["project_id"] == sample_project["id"]
        assert result["task_id"] == sample_task["id"]

    async def test_mcp_promote_invalid_project(self, db, sample_task):
        """MCP promote_task_file raises if project does not exist."""
        from switchboard.server.handlers.files_handler import _handle_promote_task_file
        fid = await self._insert_task_file(db, sample_task)

        with pytest.raises(ValueError, match="not found"):
            await _handle_promote_task_file({
                "file_id": fid,
                "project_id": "nonexistent-project",
            })

    async def test_mcp_promote_non_task_file_raises(self, db, sample_project):
        """MCP promote_task_file raises if file has no task_id."""
        from switchboard.server.handlers.files_handler import _handle_promote_task_file
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
        from switchboard.server.handlers.files_handler import _handle_promote_task_file
        fid = await self._insert_task_file(db, sample_task)

        with pytest.raises(ValueError, match="file_id is required"):
            await _handle_promote_task_file({"project_id": sample_project["id"]})

        with pytest.raises(ValueError, match="project_id is required"):
            await _handle_promote_task_file({"file_id": fid})


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


# ── get_task_status files array ─────────────────────────────────────────────


class TestGetTaskStatusFilesArray:
    """get_task_status must include a files array in both slim and detail responses."""

    async def test_slim_response_includes_files(self, db, sample_task):
        """Slim (default) response includes files array."""
        from switchboard.server.handlers.tasks import _handle_get_task_status
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


