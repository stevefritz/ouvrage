"""MCP tool handlers for files."""
import shutil
import uuid
from pathlib import Path

import switchboard.db as db
from switchboard.server.context import get_request_is_worker

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

ALLOWED_EXTENSIONS = {
    'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg',   # images
    'txt', 'md', 'json', 'csv', 'yaml', 'yml', 'toml', 'xml',  # text
    'pdf',  # documents
}

READABLE_EXTENSIONS = {
    'txt', 'md', 'json', 'csv', 'yaml', 'yml', 'toml', 'xml',
}

MIME_TYPES = {
    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
    'gif': 'image/gif', 'webp': 'image/webp', 'svg': 'image/svg+xml',
    'txt': 'text/plain', 'md': 'text/markdown', 'json': 'application/json',
    'csv': 'text/csv', 'yaml': 'application/yaml', 'yml': 'application/yaml',
    'toml': 'application/toml', 'xml': 'application/xml',
    'pdf': 'application/pdf',
}


def _is_readable(filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in READABLE_EXTENSIONS


def _uploads_dir() -> Path:
    from switchboard.config.settings import DB_PATH
    return Path(DB_PATH).parent / "uploads"


async def _handle_list_files(arguments: dict) -> dict:
    task_id = arguments.get("task_id") or None
    files = await db.list_files(task_id=task_id)
    for f in files:
        f["readable"] = _is_readable(f.get("filename", ""))
    return {"files": files}


async def _handle_get_attached_file(arguments: dict) -> dict:
    """Read the content of a text-based attached file."""
    file_id = arguments.get("file_id")
    if not file_id:
        raise ValueError("file_id is required")

    record = await db.get_file(file_id)
    if not record:
        raise ValueError(f"File '{file_id}' not found")

    filename = record.get("filename", "")
    if not _is_readable(filename):
        return {
            "error": "File is not a readable text format",
            "filename": filename,
            "mime_type": record.get("mime_type"),
            "readable": False,
        }

    stored_path = Path(record["stored_path"])
    if not stored_path.exists():
        raise ValueError(f"File not found on disk: {filename}")

    max_bytes = arguments.get("max_bytes", 1048576)
    content = stored_path.read_bytes()
    size = len(content)
    truncated = size > max_bytes

    return {
        "file_id": file_id,
        "filename": filename,
        "content": content[:max_bytes].decode("utf-8", errors="replace"),
        "size": size,
        "truncated": truncated,
        "mime_type": record.get("mime_type"),
        "task_id": record.get("task_id"),
    }


async def _handle_add_task_file(arguments: dict) -> dict:
    if not get_request_is_worker():
        raise ValueError("add_task_file is only available on the worker endpoint")

    task_id = arguments.get("task_id")
    source_path = arguments.get("source_path")
    filename = arguments.get("filename")

    if not task_id:
        raise ValueError("task_id is required")
    if not source_path:
        raise ValueError("source_path is required")

    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    worktree_path = task.get("worktree_path")
    if not worktree_path:
        raise ValueError("Task has no worktree_path — cannot validate source path")

    src = Path(source_path)
    if not src.exists():
        raise ValueError(f"File not found: {source_path}")
    if not src.is_file():
        raise ValueError(f"Source path is not a file: {source_path}")

    # Resolve real paths — prevent directory traversal
    real_src = src.resolve()
    real_worktree = Path(worktree_path).resolve()
    try:
        real_src.relative_to(real_worktree)
    except ValueError:
        raise ValueError("Source path must be within the worktree")

    # Default filename from source basename, strip any path components
    if not filename:
        filename = real_src.name
    filename = Path(filename).name
    if not filename:
        raise ValueError("Invalid filename")

    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"File type .{ext} not allowed")

    size_bytes = real_src.stat().st_size
    if size_bytes > MAX_FILE_SIZE:
        raise ValueError("File exceeds 10MB limit")

    file_id = str(uuid.uuid4())
    dest_dir = _uploads_dir() / file_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    shutil.copy2(str(real_src), str(dest))

    mime_type = MIME_TYPES.get(ext)
    record = await db.create_file(
        id=file_id,
        filename=filename,
        stored_path=str(dest),
        mime_type=mime_type,
        size_bytes=size_bytes,
        uploaded_by=None,
        task_id=task_id,
    )

    return {
        "id": record["id"],
        "filename": record["filename"],
        "stored_path": record["stored_path"],
        "size_bytes": record["size_bytes"],
    }
