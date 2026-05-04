"""LivingDocsService — thin service layer for reference doc lifecycle.

Callers (MCP handlers in task #7) should import these functions directly:
    from ouvrage.services.living_docs import set_config, add_version, ...

This module is the single entry point for living-docs business logic.
It wraps db.reference_docs.* helpers and orchestrates multi-step operations
(copy-and-embed, cascade delete) that span multiple DB tables + the filesystem.
"""

import asyncio
import os
import re
import uuid
from pathlib import Path

import ouvrage.db as db
import ouvrage.db.files as db_files
import ouvrage.db.reference_docs as db_reference_docs
from ouvrage.db.connection import get_db
from ouvrage.db._helpers import now_iso
from ouvrage.embeddings.chunks import chunk_message

LOCAL_DOCS_ROOT = Path(os.environ.get(
    "OUVRAGE_LIVING_DOCS_ROOT",
    "data/reference_docs",
))

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

_MAX_DOC_SIZE = 1024 * 1024  # 1 MB


# ---------------------------------------------------------------------------
# Config CRUD wrappers
# ---------------------------------------------------------------------------


async def set_config(
    *,
    project_id: str,
    slug: str,
    title: str,
    brief: str,
    source_hints: str | None = None,
    user_id: int | None = None,
) -> dict:
    """Upsert a reference doc config for (project_id, slug).

    Validates slug format and project existence before writing.
    Returns the upserted row.
    """
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"Invalid slug '{slug}'. Must match ^[a-z0-9][a-z0-9-]{{0,63}}$"
        )
    project = await db.get_project(project_id)
    if not project:
        raise ValueError(f"Project '{project_id}' not found")
    return await db_reference_docs.upsert_config(
        project_id=project_id,
        slug=slug,
        title=title,
        brief=brief,
        source_hints=source_hints,
        created_by=user_id,
    )


async def get_config(project_id: str, slug: str) -> dict | None:
    """Return the config row for (project_id, slug), or None if missing."""
    return await db_reference_docs.get_config(project_id, slug)


async def list_configs(project_id: str) -> list[dict]:
    """Return all config rows for a project, ordered by slug."""
    return await db_reference_docs.list_configs(project_id)


# ---------------------------------------------------------------------------
# Delete with cascade
# ---------------------------------------------------------------------------


async def delete_config(project_id: str, slug: str) -> None:
    """Delete a reference doc config and all associated data.

    Cascade order:
    1. Delete files row (+ embeddings/chunks via FK ON DELETE CASCADE).
    2. Remove local cache file.
    3. Delete config row.

    The committed .md file in the project's git repo is left untouched —
    that is the human's responsibility to clean up.

    Silent if the config does not exist.
    """
    config = await db_reference_docs.get_config(project_id, slug)
    if not config:
        return

    # Find the files row for this reference doc
    file_id = await _find_reference_doc_file_id(project_id, slug)
    if file_id:
        await db_files.delete_reference_doc_files(file_id)

    # Remove local cache file, ignoring ENOENT
    cache_path = LOCAL_DOCS_ROOT / project_id / f"{slug}.md"
    try:
        cache_path.unlink()
    except FileNotFoundError:
        pass

    await db_reference_docs.delete_config_row(config["id"])


# ---------------------------------------------------------------------------
# add_version — copy-and-embed
# ---------------------------------------------------------------------------


async def add_version(
    *,
    task_id: str,
    slug: str,
    source_path: str | Path,
) -> dict:
    """Copy a reference doc from the worktree into the local cache and queue embedding.

    Called by the worker via the add_reference_doc_version MCP tool (#7).
    The MCP handler is responsible for the worker auth check.

    The config for (project_id, slug) must already exist — create it with
    set_reference_doc_config before calling this function.

    Idempotent: re-calling for the same (project_id, slug) overwrites the
    local cache file and updates the existing files row.

    Returns:
        {
            "file_id": str,
            "stored_path": str (absolute path to local cache),
            "embedded": "queued",
            "chunkable": bool,
        }
    """
    # --- 1. Resolve task ---
    task = await db.get_task(task_id)
    if not task:
        raise ValueError(f"Task '{task_id}' not found")

    project_id: str = task["project_id"]
    worktree_path: str | None = task.get("worktree_path")
    if not worktree_path:
        raise ValueError(f"Task '{task_id}' has no worktree_path")

    # --- 2. Validate source_path (resolve + relative_to, prevent traversal) ---
    src = Path(source_path)
    if not src.exists():
        raise ValueError(f"File not found: {source_path}")
    if not src.is_file():
        raise ValueError(f"Source path is not a file: {source_path}")

    real_src = src.resolve()
    real_worktree = Path(worktree_path).resolve()
    try:
        real_src.relative_to(real_worktree)
    except ValueError:
        raise ValueError("Source path must be within the worktree")

    # --- 3. Validate file properties ---
    if real_src.suffix.lower() != ".md":
        raise ValueError(f"Source file must be a .md file, got: {real_src.name}")

    size_bytes = real_src.stat().st_size
    if size_bytes >= _MAX_DOC_SIZE:
        raise ValueError(
            f"File too large: {size_bytes} bytes (max {_MAX_DOC_SIZE} bytes)"
        )

    # --- 4. Config must exist ---
    config = await db_reference_docs.get_config(project_id, slug)
    if not config:
        raise ValueError(
            f"Reference doc config '{slug}' not found in project '{project_id}'. "
            "Configure it via set_reference_doc_config first."
        )

    # --- 5. Read content ---
    content = real_src.read_text(encoding="utf-8", errors="replace")

    # --- 6. Atomic copy to local cache ---
    target = LOCAL_DOCS_ROOT / project_id / f"{slug}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(target)

    # --- 7. Upsert files row keyed by (project_id, role='reference_doc', filename='{slug}.md') ---
    file_id = await _upsert_reference_doc_file(
        project_id=project_id,
        slug=slug,
        stored_path=str(target.resolve()),
        size_bytes=size_bytes,
    )

    # --- 8. Fire-and-forget embedding ---
    asyncio.create_task(db.index_doc_file(file_id))

    # --- 9. Chunkability check (informational only, does NOT raise) ---
    chunks = chunk_message(content)
    chunkable = chunks is not None
    if not chunkable:
        await db.post_task_message(
            task_id=task_id,
            author="system",
            type="note",
            content=(
                f"Reference doc '{slug}' has insufficient structure for chunk-level "
                "semantic search (too short, no markdown headers, or single section). "
                "Whole-file embedding will still be indexed and searchable."
            ),
        )

    return {
        "file_id": file_id,
        "stored_path": str(target.resolve()),
        "embedded": "queued",
        "chunkable": chunkable,
    }


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


async def get_local_copy(project_id: str, slug: str) -> str | None:
    """Return text of the local cache file, or None if it doesn't exist."""
    cache_path = LOCAL_DOCS_ROOT / project_id / f"{slug}.md"
    try:
        return cache_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None


async def list_runs(project_id: str, limit: int = 20) -> list[dict]:
    """Return reference doc runs for a project, most recent first."""
    return await db_reference_docs.list_runs(project_id, limit)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _find_reference_doc_file_id(project_id: str, slug: str) -> str | None:
    """Look up the files row for (project_id, slug) reference doc. Returns id or None."""
    async with get_db() as conn:
        rows = await conn.execute_fetchall(
            "SELECT id FROM files WHERE project_id = ? AND role = 'reference_doc' AND filename = ?",
            (project_id, f"{slug}.md"),
        )
    return rows[0]["id"] if rows else None


async def _upsert_reference_doc_file(
    *,
    project_id: str,
    slug: str,
    stored_path: str,
    size_bytes: int,
) -> str:
    """Upsert the files row for a reference doc. Returns the file_id.

    Upsert key: (project_id, role='reference_doc', filename='{slug}.md').
    If found: update stored_path, size_bytes, updated_at.
    If not found: insert via db_files.create_file with role='reference_doc'.
    """
    existing_id = await _find_reference_doc_file_id(project_id, slug)
    if existing_id:
        ts = now_iso()
        async with get_db() as conn:
            await conn.execute(
                "UPDATE files SET stored_path = ?, size_bytes = ?, updated_at = ? WHERE id = ?",
                (stored_path, size_bytes, ts, existing_id),
            )
            await conn.commit()
        return existing_id

    file_id = str(uuid.uuid4())
    await db_files.create_file(
        id=file_id,
        filename=f"{slug}.md",
        stored_path=stored_path,
        mime_type="text/markdown",
        size_bytes=size_bytes,
        uploaded_by=None,
        project_id=project_id,
        role="reference_doc",
    )
    return file_id
