"""Reverse proxy for the Anthropic API.

CC workers call through this proxy so the real API key never enters
the worker subprocess environment.  The proxy decrypts the key from
Fernet storage and injects it as the ``X-Api-Key`` header on every
outgoing request.

Only accessible from localhost (same trust boundary as ``/mcp/worker``).
"""

import json
import logging

import httpx

import switchboard.db as db

log = logging.getLogger("switchboard.server.proxy")

ANTHROPIC_API_BASE = "https://api.anthropic.com"

# Headers that must NOT be forwarded from the worker request.
_STRIP_HEADERS = frozenset({
    b"host",
    b"x-api-key",
    b"authorization",
    b"accept-encoding",  # prevent compressed responses that break streaming
})


async def _read_body(receive) -> bytes:
    """Read the full ASGI request body."""
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    return body


async def _send_error(send, status: int, detail: str) -> None:
    """Send a JSON error response."""
    payload = json.dumps({"error": detail}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            [b"content-type", b"application/json"],
        ],
    })
    await send({"type": "http.response.body", "body": payload})


async def _resolve_api_key(user_id: int | None) -> str | None:
    """Resolve the Anthropic API key for the given user, falling back to instance owner."""
    if user_id is not None:
        try:
            return await db.get_anthropic_key(user_id)
        except (ValueError, TypeError):
            pass

    # Fallback to instance owner
    try:
        instance = await db.get_instance()
        owner_id = instance.get("owner_user_id") if instance else None
        if owner_id:
            return await db.get_anthropic_key(int(owner_id))
    except (ValueError, TypeError):
        pass
    return None


async def handle_anthropic_proxy(scope, receive, send) -> None:
    """ASGI handler that proxies requests to the Anthropic API.

    Route pattern: ``/proxy/anthropic/{user_id}/...``

    The ``{user_id}`` path segment identifies whose encrypted API key to
    decrypt.  Everything after is forwarded as the upstream path
    (e.g. ``/v1/messages``).
    """
    # ── Localhost gate ────────────────────────────────────────────────
    client = scope.get("client")
    if not client or client[0] not in ("127.0.0.1", "::1"):
        await _send_error(send, 403, "Proxy only accessible from localhost")
        return

    # ── Parse path ────────────────────────────────────────────────────
    # Expected: /proxy/anthropic/{user_id}/v1/messages
    path = scope.get("path", "")
    prefix = "/proxy/anthropic/"
    remainder = path[len(prefix):]  # e.g. "42/v1/messages"

    # Split user_id from the rest
    parts = remainder.split("/", 1)
    try:
        user_id = int(parts[0])
    except (ValueError, IndexError):
        user_id = None

    upstream_path = "/" + parts[1] if len(parts) > 1 else "/"

    # ── Health check ─────────────────────────────────────────────────
    # CC sends HEAD to ANTHROPIC_BASE_URL to validate the endpoint.
    # Bare path (no /v1/... suffix) → return 200 without forwarding.
    if upstream_path == "/":
        api_key = await _resolve_api_key(user_id)
        status = 200 if api_key else 502
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [[b"content-type", b"application/json"]],
        })
        await send({"type": "http.response.body", "body": b'{"ok":true}' if api_key else b'{"error":"no api key"}'})
        return

    # ── Resolve API key ──────────────────────────────────────────────
    api_key = await _resolve_api_key(user_id)
    if not api_key:
        await _send_error(send, 502, "Anthropic API key not configured")
        return

    # ── Build upstream request ───────────────────────────────────────
    # Forward headers, stripping auth-related ones
    upstream_headers = {}
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() in _STRIP_HEADERS:
            continue
        name = raw_name.decode("latin-1")
        value = raw_value.decode("latin-1")
        upstream_headers[name] = value

    # Inject credentials
    upstream_headers["x-api-key"] = api_key
    upstream_headers["anthropic-version"] = upstream_headers.get(
        "anthropic-version", "2023-06-01"
    )
    upstream_headers["host"] = "api.anthropic.com"

    # Include query string if present
    query_string = scope.get("query_string", b"")
    upstream_url = f"{ANTHROPIC_API_BASE}{upstream_path}"
    if query_string:
        upstream_url += f"?{query_string.decode('latin-1')}"

    method = scope.get("method", "POST")

    # Read request body
    body = await _read_body(receive)

    # ── Forward to Anthropic with streaming response ─────────────────
    # Force identity encoding so Anthropic returns uncompressed responses.
    # The SDK expects plaintext JSON from ANTHROPIC_BASE_URL.
    upstream_headers["accept-encoding"] = "identity"

    async with httpx.AsyncClient() as client_http:
        try:
            async with client_http.stream(
                method,
                upstream_url,
                headers=upstream_headers,
                content=body,
                timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
            ) as upstream_resp:
                # Build response headers, stripping hop-by-hop and encoding headers
                resp_headers = []
                for name, value in upstream_resp.headers.multi_items():
                    lower_name = name.lower()
                    if lower_name in (
                        "transfer-encoding", "connection", "keep-alive",
                        "content-encoding",
                    ):
                        continue
                    resp_headers.append([
                        name.encode("latin-1"),
                        value.encode("latin-1"),
                    ])

                await send({
                    "type": "http.response.start",
                    "status": upstream_resp.status_code,
                    "headers": resp_headers,
                })

                # Stream body chunks as they arrive (raw, uncompressed)
                async for chunk in upstream_resp.aiter_raw():
                    await send({
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    })
                # Signal end of body
                await send({
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False,
                })
        except httpx.ConnectError as exc:
            await _send_error(send, 502, f"Failed to connect to Anthropic API: {exc}")
        except httpx.TimeoutException as exc:
            await _send_error(send, 504, f"Anthropic API request timed out: {exc}")
        except Exception as exc:
            log.exception("Unexpected error proxying to Anthropic API")
            await _send_error(send, 502, f"Proxy error: {exc}")
