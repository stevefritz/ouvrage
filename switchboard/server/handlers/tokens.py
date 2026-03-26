"""API token management tool handlers."""

import switchboard.db as db
from switchboard.server.context import get_request_user_id


async def _handle_create_api_token(arguments):
    user_id = get_request_user_id()
    if not user_id:
        return {"error": "Authentication required — no user resolved for this request"}
    name = arguments.get("name")
    result = await db.create_api_token(user_id=user_id, name=name)
    return {"token": result["token"], "id": result["id"], "name": result["name"]}


async def _handle_list_api_tokens(arguments):
    user_id = get_request_user_id()
    if not user_id:
        return {"error": "Authentication required — no user resolved for this request"}
    tokens = await db.list_api_tokens(user_id=user_id)
    return {"tokens": tokens}


async def _handle_revoke_api_token(arguments):
    user_id = get_request_user_id()
    if not user_id:
        return {"error": "Authentication required — no user resolved for this request"}
    token_id = arguments["token_id"]
    deleted = await db.revoke_api_token(token_id)
    if not deleted:
        return {"error": f"Token {token_id} not found"}
    return {"revoked": True, "token_id": token_id}
