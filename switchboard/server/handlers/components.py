"""Component and control tool handlers."""

import switchboard.db as db
import switchboard.dispatch as task_engine
from switchboard.server.context import get_request_user_id


async def _handle_create_component(arguments):
    component_id = arguments["id"]
    project_id = arguments["project_id"]
    name = arguments["name"]
    extras = {k: v for k, v in arguments.items() if k in ("description", "phase", "review_ignore_patterns")}
    return await db.create_component(
        id=component_id, project_id=project_id, name=name,
        created_by=get_request_user_id(), **extras,
    )


async def _handle_update_component(arguments):
    component_id = arguments["id"]
    allowed = {"name", "description", "phase", "review_ignore_patterns"}
    fields = {k: v for k, v in arguments.items() if k in allowed}
    if not fields:
        return {"error": "No fields to update"}
    return await db.update_component(component_id, **fields)


async def _handle_get_component(arguments):
    result = await db.get_component(arguments["id"])
    return result if result else {"error": f"Component '{arguments['id']}' not found"}


async def _handle_list_components(arguments):
    return await db.list_components(project_id=arguments.get("project_id"))


async def _handle_link_conversation(arguments):
    return await db.link_conversation(
        component_id=arguments["component_id"],
        conversation_id=arguments["conversation_id"],
    )


async def _handle_unlink_conversation(arguments):
    return await db.unlink_conversation(
        component_id=arguments["component_id"],
        conversation_id=arguments["conversation_id"],
    )


async def _handle_search_component(arguments):
    return await db.search_component(
        component_id=arguments["component_id"],
        query=arguments["query"],
        include_graphiti=arguments.get("include_graphiti", False),
        limit=arguments.get("limit", 20),
    )


async def _handle_pause_component(arguments):
    return await task_engine.pause_component(arguments["component_id"])


async def _handle_resume_component(arguments):
    return await task_engine.resume_component(arguments["component_id"])


async def _handle_stop_component(arguments):
    return await task_engine.stop_component(arguments["component_id"])


