"""Tests for the Anthropic API reverse proxy and worker env changes."""

import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

import ouvrage.db as db
from ouvrage.server.proxy import handle_anthropic_proxy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_scope(path="/proxy/anthropic/1/v1/messages", method="POST",
                client=("127.0.0.1", 54321), headers=None):
    """Build a minimal ASGI HTTP scope dict."""
    raw_headers = headers or []
    if not any(n == b"content-type" for n, _ in raw_headers):
        raw_headers.append([b"content-type", b"application/json"])
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": raw_headers,
        "client": client,
    }


def _make_receive(body: bytes = b'{"model":"claude-sonnet-4-20250514","messages":[]}'):
    """Return an ASGI receive callable that yields the given body."""
    called = False

    async def receive():
        nonlocal called
        if not called:
            called = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}
    return receive


class _ResponseCollector:
    """Collects ASGI send() calls for assertions."""

    def __init__(self):
        self.start = None
        self.body_parts: list[bytes] = []
        self._calls: list[dict] = []

    async def __call__(self, message):
        self._calls.append(message)
        if message["type"] == "http.response.start":
            self.start = message
        elif message["type"] == "http.response.body":
            self.body_parts.append(message.get("body", b""))

    @property
    def status(self) -> int:
        return self.start["status"] if self.start else 0

    @property
    def body(self) -> bytes:
        return b"".join(self.body_parts)

    @property
    def headers_dict(self) -> dict:
        """Response headers as a lowercase-keyed dict."""
        if not self.start:
            return {}
        return {
            n.decode("latin-1").lower(): v.decode("latin-1")
            for n, v in self.start.get("headers", [])
        }


def _mock_httpx_stream(captured_request, status_code=200, response_headers=None,
                       response_chunks=None):
    """Build a mock httpx.AsyncClient that captures requests and returns canned responses.

    Returns a patch context manager for ``ouvrage.server.proxy.httpx.AsyncClient``.
    """
    if response_headers is None:
        response_headers = [("content-type", "application/json")]
    if response_chunks is None:
        response_chunks = [b'{"ok":true}']

    def _stream(method, url, **kwargs):
        captured_request["method"] = method
        captured_request["url"] = str(url)
        captured_request["headers"] = kwargs.get("headers", {})
        captured_request["content"] = kwargs.get("content", b"")

        resp = MagicMock()
        resp.status_code = status_code
        resp.headers = MagicMock()
        resp.headers.multi_items = MagicMock(return_value=response_headers)

        async def aiter_raw():
            for chunk in response_chunks:
                yield chunk
        resp.aiter_raw = aiter_raw

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    mock_client = MagicMock()
    mock_client.stream = _stream
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    return patch(
        "ouvrage.server.proxy.httpx.AsyncClient",
        return_value=mock_client,
    )


# ---------------------------------------------------------------------------
# Proxy tests
# ---------------------------------------------------------------------------


class TestAnthropicProxy:
    """Tests for handle_anthropic_proxy."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        """Set up a user with an Anthropic API key."""
        self.user = await db.create_user(
            email="proxy-test@example.com", name="Proxy Test", password_hash="x"
        )
        await db.update_user_credentials(self.user["id"], anthropic_api_key="sk-ant-test-key-12345")

    async def test_proxy_injects_api_key_header(self, db):
        """Proxy decrypts the API key and injects X-Api-Key header."""
        captured = {}
        user_id = self.user["id"]
        scope = _make_scope(path=f"/proxy/anthropic/{user_id}/v1/messages")

        with _mock_httpx_stream(captured, response_chunks=[b'{"id":"msg_123"}']):
            await handle_anthropic_proxy(scope, _make_receive(), _ResponseCollector())

        assert captured["headers"]["x-api-key"] == "sk-ant-test-key-12345"
        assert captured["headers"]["anthropic-version"] == "2023-06-01"
        assert captured["url"] == "https://api.anthropic.com/v1/messages"

    async def test_proxy_strips_existing_auth_headers(self, db):
        """Proxy strips X-Api-Key and Authorization from worker request."""
        captured = {}
        user_id = self.user["id"]
        scope = _make_scope(
            path=f"/proxy/anthropic/{user_id}/v1/messages",
            headers=[
                [b"content-type", b"application/json"],
                [b"x-api-key", b"stolen-key"],
                [b"authorization", b"Bearer stolen-token"],
            ],
        )

        with _mock_httpx_stream(captured):
            await handle_anthropic_proxy(scope, _make_receive(), _ResponseCollector())

        assert captured["headers"]["x-api-key"] == "sk-ant-test-key-12345"
        assert "authorization" not in captured["headers"]

    async def test_proxy_streams_sse_response(self, db):
        """Proxy streams SSE chunks without buffering."""
        user_id = self.user["id"]
        scope = _make_scope(path=f"/proxy/anthropic/{user_id}/v1/messages")
        send = _ResponseCollector()

        sse_chunks = [
            b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n",
            b"event: content_block_delta\ndata: {\"type\":\"content_block_delta\"}\n\n",
            b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
        ]

        with _mock_httpx_stream(
            {},
            response_headers=[("content-type", "text/event-stream")],
            response_chunks=sse_chunks,
        ):
            await handle_anthropic_proxy(scope, _make_receive(), send)

        assert send.status == 200
        assert send.headers_dict.get("content-type") == "text/event-stream"
        # 3 SSE chunks + 1 empty final
        assert len(send.body_parts) == 4
        assert send.body_parts[0] == sse_chunks[0]
        assert send.body_parts[1] == sse_chunks[1]
        assert send.body_parts[2] == sse_chunks[2]
        assert send.body_parts[3] == b""

    async def test_proxy_rejects_non_localhost(self, db):
        """Proxy rejects requests from non-localhost addresses."""
        scope = _make_scope(client=("192.168.1.100", 54321))
        send = _ResponseCollector()

        await handle_anthropic_proxy(scope, _make_receive(), send)

        assert send.status == 403
        body = json.loads(send.body)
        assert "localhost" in body["error"].lower()

    async def test_proxy_error_when_no_api_key(self, db):
        """Proxy returns 502 when no API key is configured for the user."""
        other_user = await db.create_user(
            email="no-key@example.com", name="No Key", password_hash="x"
        )
        scope = _make_scope(path=f"/proxy/anthropic/{other_user['id']}/v1/messages")
        send = _ResponseCollector()

        await handle_anthropic_proxy(scope, _make_receive(), send)

        assert send.status == 502
        body = json.loads(send.body)
        assert "not configured" in body["error"].lower()

    async def test_proxy_passes_through_upstream_errors(self, db):
        """Proxy passes through error responses from Anthropic API."""
        user_id = self.user["id"]
        scope = _make_scope(path=f"/proxy/anthropic/{user_id}/v1/messages")
        send = _ResponseCollector()

        error_body = b'{"error":{"type":"rate_limit_error","message":"Rate limited"}}'
        with _mock_httpx_stream(
            {},
            status_code=429,
            response_headers=[("content-type", "application/json"), ("retry-after", "30")],
            response_chunks=[error_body],
        ):
            await handle_anthropic_proxy(scope, _make_receive(), send)

        assert send.status == 429

    async def test_proxy_preserves_query_string(self, db):
        """Proxy forwards query string to upstream."""
        captured = {}
        user_id = self.user["id"]
        scope = _make_scope(path=f"/proxy/anthropic/{user_id}/v1/messages")
        scope["query_string"] = b"beta=true"

        with _mock_httpx_stream(captured):
            await handle_anthropic_proxy(scope, _make_receive(), _ResponseCollector())

        assert captured["url"] == "https://api.anthropic.com/v1/messages?beta=true"


# ---------------------------------------------------------------------------
# Worker env tests
# ---------------------------------------------------------------------------


class TestWorkerEnvAnthropicProxy:
    """Verify worker env no longer contains ANTHROPIC_API_KEY and sets ANTHROPIC_BASE_URL."""

    @pytest.fixture(autouse=True)
    async def _setup(self, db):
        self.db = db
        self.user = await db.create_user(
            email="env-test@example.com", name="Env Test", password_hash="x"
        )
        await db.update_user_credentials(self.user["id"], anthropic_api_key="sk-ant-secret")
        self.project = await db.create_project(
            id="env-test-proj",
            repo="https://github.com/test/repo.git",
            working_dir="/tmp/env-test",
            created_by=self.user["id"],
        )

    async def test_sdk_session_env_has_base_url_not_api_key(self, db):
        """_run_sdk_session sets ANTHROPIC_BASE_URL instead of ANTHROPIC_API_KEY."""
        task_id = "env-test-proj/test-env-1"
        await db.create_task(
            id=task_id, project_id="env-test-proj", goal="test env",
            dispatched_by=self.user["id"],
        )
        await db.update_task(task_id, status="working")

        captured_env = {}

        def capture_options(*args, **kwargs):
            if "env" in kwargs:
                captured_env.update(kwargs["env"])
            return MagicMock()

        with patch("ouvrage.dispatch.sdk_session.ClaudeAgentOptions", side_effect=capture_options), \
             patch("ouvrage.dispatch.sdk_session._run_as_worker", new_callable=AsyncMock) as mock_run, \
             patch("ouvrage.dispatch.sdk_session.SKIP_CREDENTIAL_CHECK", False), \
             patch("ouvrage.dispatch.sdk_session.WORKER_USER", "nobody"):
            mock_run.return_value = (b"no", b"", 0)

            from ouvrage.dispatch.sdk_session import _run_sdk_session
            try:
                await _run_sdk_session(
                    task_id=task_id,
                    prompt="test",
                    worktree_path="/tmp/fake",
                    session_id=None,
                    is_resume=False,
                    max_turns=10,
                    max_wall_clock_minutes=5,
                    log_dir=MagicMock(),
                    model="sonnet",
                )
            except Exception:
                pass

        assert "ANTHROPIC_API_KEY" not in captured_env
        assert "ANTHROPIC_BASE_URL" in captured_env
        assert "/proxy/anthropic/" in captured_env["ANTHROPIC_BASE_URL"]
        assert str(self.user["id"]) in captured_env["ANTHROPIC_BASE_URL"]

    async def test_gate_env_has_base_url_not_api_key(self, db, tmp_path):
        """Gate subprocess sets ANTHROPIC_BASE_URL instead of ANTHROPIC_API_KEY."""
        worktree_dir = tmp_path / "fake-wt"
        worktree_dir.mkdir()

        task_id = "env-test-proj/test-env-2"
        await db.create_task(
            id=task_id, project_id="env-test-proj", goal="test gate env",
            dispatched_by=self.user["id"],
        )
        await db.update_task(
            task_id, status="working",
            worktree_path=str(worktree_dir),
        )

        captured_env = {}

        def capture_options(*args, **kwargs):
            if "env" in kwargs:
                captured_env.update(kwargs["env"])
            return MagicMock()

        with patch("ouvrage.dispatch.gates.ClaudeAgentOptions", side_effect=capture_options), \
             patch("ouvrage.dispatch.gates.SKIP_CREDENTIAL_CHECK", False), \
             patch("ouvrage.dispatch.gates.WORKER_USER", "nobody"), \
             patch("ouvrage.dispatch.gates.pwd") as mock_pwd, \
             patch("ouvrage.dispatch.gates._open_shared", return_value=MagicMock()), \
             patch("ouvrage.dispatch.gates.ClaudeSDKClient") as mock_client_cls:
            mock_pwd.getpwnam.return_value = MagicMock(pw_dir="/tmp/fake-home")

            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.process_streaming = MagicMock(return_value=AsyncMock(return_value=[])())
            mock_client_cls.return_value = mock_client

            from ouvrage.dispatch.gates import _run_subtask
            try:
                await _run_subtask(
                    task_id=task_id,
                    subtask_type="test",
                    prompt="test prompt",
                    model="sonnet",
                    max_turns=5,
                )
            except Exception:
                pass

        assert "ANTHROPIC_API_KEY" not in captured_env
        assert "ANTHROPIC_BASE_URL" in captured_env
        assert "/proxy/anthropic/" in captured_env["ANTHROPIC_BASE_URL"]
