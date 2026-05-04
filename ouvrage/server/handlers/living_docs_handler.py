"""MCP tool handlers for Living Docs reference doc management."""

import ouvrage.db as db
from ouvrage.server.context import get_request_is_worker
from ouvrage.services.living_docs import (
    add_version,
    delete_config,
    get_config,
    get_local_copy,
    list_configs,
    set_config,
)


async def _handle_set_reference_doc_config(arguments: dict) -> dict:
    project_id = arguments.get("project_id")
    slug = arguments.get("slug")
    title = arguments.get("title")
    brief = arguments.get("brief")
    source_hints = arguments.get("source_hints") or None

    if not project_id:
        raise ValueError("project_id is required")
    if not slug:
        raise ValueError("slug is required")
    if not title:
        raise ValueError("title is required")
    if not brief:
        raise ValueError("brief is required")

    row = await set_config(
        project_id=project_id,
        slug=slug,
        title=title,
        brief=brief,
        source_hints=source_hints,
    )
    return dict(row)


async def _handle_delete_reference_doc_config(arguments: dict) -> dict:
    project_id = arguments.get("project_id")
    slug = arguments.get("slug")

    if not project_id:
        raise ValueError("project_id is required")
    if not slug:
        raise ValueError("slug is required")

    await delete_config(project_id, slug)
    return {"deleted": True}


async def _handle_set_living_docs_enabled(arguments: dict) -> dict:
    project_id = arguments.get("project_id")
    enabled = arguments.get("enabled")

    if not project_id:
        raise ValueError("project_id is required")
    if enabled is None:
        raise ValueError("enabled is required")

    project = await db.get_project(project_id)
    if not project:
        raise ValueError(f"Project '{project_id}' not found")

    await db.update_project(project_id, living_docs_enabled=enabled)
    return {"enabled": bool(enabled)}


async def _handle_add_reference_doc_version(arguments: dict) -> dict:
    if not get_request_is_worker():
        raise ValueError("add_reference_doc_version is only available on the worker endpoint")

    task_id = arguments.get("task_id")
    slug = arguments.get("slug")
    source_path = arguments.get("source_path")

    if not task_id:
        raise ValueError("task_id is required")
    if not slug:
        raise ValueError("slug is required")
    if not source_path:
        raise ValueError("source_path is required")

    return await add_version(task_id=task_id, slug=slug, source_path=source_path)


async def _handle_list_reference_doc_configs(arguments: dict) -> dict:
    project_id = arguments.get("project_id")

    if not project_id:
        raise ValueError("project_id is required")

    configs = await list_configs(project_id)
    return {"configs": [dict(c) for c in configs]}


async def _handle_get_reference_doc_config(arguments: dict) -> dict:
    project_id = arguments.get("project_id")
    slug = arguments.get("slug")

    if not project_id:
        raise ValueError("project_id is required")
    if not slug:
        raise ValueError("slug is required")

    config = await get_config(project_id, slug)
    if not config:
        raise ValueError(f"Reference doc config '{slug}' not found in project '{project_id}'")

    local_copy = await get_local_copy(project_id, slug)
    result = dict(config)
    result["local_copy_present"] = local_copy is not None
    return result
