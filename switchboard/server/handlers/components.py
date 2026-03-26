"""Component and control tool handlers."""

import database as db
import tasks as task_engine


async def _handle_create_component(arguments):
    component_id = arguments.pop("id")
    project_id = arguments.pop("project_id")
    name = arguments.pop("name")
    return await db.create_component(
        id=component_id, project_id=project_id, name=name, **arguments,
    )


async def _handle_update_component(arguments):
    component_id = arguments.pop("id")
    if not arguments:
        return {"error": "No fields to update"}
    return await db.update_component(component_id, **arguments)


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


async def _handle_pause_project(arguments):
    return await task_engine.pause_project(arguments["project_id"])


async def _handle_resume_project(arguments):
    return await task_engine.resume_project(arguments["project_id"])


async def _handle_stop_project(arguments):
    return await task_engine.stop_project(arguments["project_id"])
