"""Tests for three-layer credential validation:

- validate_project_access: shared validation function
- Dispatch pre-flight gate: holds tasks on credential failure
- Settings test endpoint: scope checking
- Dead code removal verification
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from switchboard.git.providers.base import ValidationResult


# ── Layer 2: validate_project_access ──────────────────────────────────────


class TestValidateProjectAccess:
    """validate_project_access must resolve credential + call provider."""

    @pytest.fixture(autouse=True)
    def _restore_real_validate(self):
        """Override autouse mock with real function so we can test actual behavior."""
        from conftest import _real_validate_project_access
        with patch("switchboard.git.validation.validate_project_access", _real_validate_project_access):
            yield

    async def test_validated_with_valid_credential(self, db):
        """Valid credential → status 'validated' with message."""
        from switchboard.git.validation import validate_project_access
        from switchboard.git.providers.github import GitHubProvider

        mock_provider = MagicMock(spec=GitHubProvider)
        mock_provider.parse_repo_url.return_value = MagicMock(owner="acme", repo="widgets", hostname="github.com")
        mock_provider.validate_access = AsyncMock(return_value=ValidationResult(valid=True, username="octocat"))

        with patch("switchboard.git.validation.resolve_credential", return_value=(mock_provider, "ghp_test")):
            result = await validate_project_access({
                "id": "test-project",
                "repo": "https://github.com/acme/widgets.git",
                "provider": "github",
            })

        assert result["status"] == "validated"
        assert "octocat" in result["message"]
        assert result["checked_at"] is not None

    async def test_warning_when_no_credential(self, db):
        """No credential configured → status 'warning'."""
        from switchboard.git.validation import validate_project_access

        with patch("switchboard.git.validation.resolve_credential", side_effect=ValueError("No github credential configured")):
            result = await validate_project_access({
                "id": "test-project",
                "repo": "https://github.com/acme/widgets.git",
                "provider": "github",
            })

        assert result["status"] == "warning"
        assert "No credential" in result["message"]

    async def test_error_when_credential_invalid(self, db):
        """Credential exists but is invalid → status 'error'."""
        from switchboard.git.validation import validate_project_access
        from switchboard.git.providers.github import GitHubProvider

        mock_provider = MagicMock(spec=GitHubProvider)
        mock_provider.parse_repo_url.return_value = MagicMock(owner="acme", repo="widgets", hostname="github.com")
        mock_provider.validate_access = AsyncMock(return_value=ValidationResult(valid=False, error="PAT is invalid or lacks permissions"))

        with patch("switchboard.git.validation.resolve_credential", return_value=(mock_provider, "ghp_bad")):
            result = await validate_project_access({
                "id": "test-project",
                "repo": "https://github.com/acme/widgets.git",
                "provider": "github",
            })

        assert result["status"] == "error"
        assert "invalid" in result["message"].lower() or "lacks" in result["message"].lower()

    async def test_error_when_repo_url_unparseable(self, db):
        """Unparseable repo URL → status 'error'."""
        from switchboard.git.validation import validate_project_access
        from switchboard.git.providers.github import GitHubProvider

        mock_provider = MagicMock(spec=GitHubProvider)
        mock_provider.parse_repo_url.side_effect = ValueError("Cannot parse URL")

        with patch("switchboard.git.validation.resolve_credential", return_value=(mock_provider, "ghp_test")):
            result = await validate_project_access({
                "id": "test-project",
                "repo": "not-a-valid-url",
                "provider": "github",
            })

        assert result["status"] == "error"
        assert "parse" in result["message"].lower()

    async def test_error_when_network_failure(self, db):
        """Network error during validation → status 'error'."""
        from switchboard.git.validation import validate_project_access
        from switchboard.git.providers.github import GitHubProvider

        mock_provider = MagicMock(spec=GitHubProvider)
        mock_provider.parse_repo_url.return_value = MagicMock(owner="acme", repo="widgets", hostname="github.com")
        mock_provider.validate_access = AsyncMock(side_effect=Exception("Connection refused"))

        with patch("switchboard.git.validation.resolve_credential", return_value=(mock_provider, "ghp_test")):
            result = await validate_project_access({
                "id": "test-project",
                "repo": "https://github.com/acme/widgets.git",
                "provider": "github",
            })

        assert result["status"] == "error"
        assert "Connection refused" in result["message"]


# ── Layer 2: Schema fields stored on project ─────────────────────────────


class TestProjectCredentialStatusFields:
    """credential_status fields persist on project rows."""

    async def test_create_project_stores_credential_status(self, db):
        """After create + validation, project row has credential_status fields."""
        from switchboard.server.handlers.projects import _handle_create_project

        mock_validate = AsyncMock(return_value={
            "status": "warning",
            "message": "No credential configured",
            "checked_at": "2026-01-01T00:00:00Z",
        })

        with patch("switchboard.server.handlers.projects.WORKTREE_BASE", "/work"):
            with patch("switchboard.git.validation.validate_project_access", mock_validate):
                result = await _handle_create_project({
                    "id": "cred-status-test",
                    "repo": "https://github.com/acme/test.git",
                    "model": "sonnet",
                    "review_model": "sonnet",
                    "auto_test": True,
                    "auto_review": True,
                    "auto_pr": False,
                    "auto_merge": False,
                    "max_turns": 100,
                    "max_wall_clock": 30,
                })

        assert "error" not in result
        project = await db.get_project("cred-status-test")
        assert project["credential_status"] == "warning"
        assert project["credential_status_message"] == "No credential configured"

    async def test_update_project_revalidates_on_credential_change(self, db):
        """Updating credential_override triggers re-validation."""
        from switchboard.server.handlers.projects import _handle_create_project, _handle_update_project

        mock_validate = AsyncMock(return_value={
            "status": "warning",
            "message": "No credential",
            "checked_at": "2026-01-01T00:00:00Z",
        })

        with patch("switchboard.server.handlers.projects.WORKTREE_BASE", "/work"):
            with patch("switchboard.git.validation.validate_project_access", mock_validate):
                await _handle_create_project({
                    "id": "revalidate-test",
                    "repo": "https://github.com/acme/test.git",
                    "model": "sonnet",
                    "review_model": "sonnet",
                    "auto_test": True,
                    "auto_review": True,
                    "auto_pr": False,
                    "auto_merge": False,
                    "max_turns": 100,
                    "max_wall_clock": 30,
                })

        # Now update with a credential override — should trigger revalidation
        mock_validate_2 = AsyncMock(return_value={
            "status": "validated",
            "message": "Credential validated (as octocat)",
            "checked_at": "2026-01-01T00:01:00Z",
        })

        with patch("switchboard.git.validation.validate_project_access", mock_validate_2):
            result = await _handle_update_project({
                "id": "revalidate-test",
                "credential_override": "ghp_new_token_123456",
            })

        assert result["credential_status"] == "validated"


# ── Layer 3: Dispatch pre-flight gate ─────────────────────────────────────


class TestDispatchPreflightGate:
    """Dispatch must hold tasks when credential validation fails."""

    @pytest.fixture(autouse=True)
    def _patches(self, mock_git):
        """Patch git and SDK operations to avoid real calls."""

    async def test_dispatch_holds_task_on_missing_credential(self, db, sample_project):
        """Missing credential → task held, not failed."""
        from switchboard.dispatch.lifecycle import _dispatch_launch_session

        task = await db.create_task(
            id="test-project/preflight-missing",
            project_id="test-project",
            goal="Test pre-flight",
        )
        await db.update_task(task["id"], status="working")

        mock_validate = AsyncMock(return_value={
            "status": "warning",
            "message": "No github credential configured.",
            "checked_at": "2026-01-01T00:00:00Z",
        })

        with patch("switchboard.git.validation.validate_project_access", mock_validate):
            await _dispatch_launch_session(task)

        updated = await db.get_task("test-project/preflight-missing")
        assert updated["status"] == "held"
        assert updated["held"] == 1

        # Should have posted a message
        messages_result = await db.read_task_messages("test-project/preflight-missing")
        messages = messages_result.get("messages", []) if isinstance(messages_result, dict) else messages_result
        held_msgs = [m for m in messages if "credential" in (m.get("content") or "").lower()]
        assert len(held_msgs) > 0

    async def test_dispatch_holds_task_on_invalid_credential(self, db, sample_project):
        """Invalid credential → task held with actionable error."""
        from switchboard.dispatch.lifecycle import _dispatch_launch_session

        task = await db.create_task(
            id="test-project/preflight-invalid",
            project_id="test-project",
            goal="Test pre-flight invalid",
        )
        await db.update_task(task["id"], status="working")

        mock_validate = AsyncMock(return_value={
            "status": "error",
            "message": "PAT is invalid or lacks permissions",
            "checked_at": "2026-01-01T00:00:00Z",
        })

        with patch("switchboard.git.validation.validate_project_access", mock_validate):
            await _dispatch_launch_session(task)

        updated = await db.get_task("test-project/preflight-invalid")
        assert updated["status"] == "held"

    async def test_dispatch_proceeds_on_valid_credential(self, db, sample_project):
        """Valid credential → dispatch proceeds past pre-flight (not held)."""
        from switchboard.dispatch.lifecycle import _dispatch_launch_session

        task = await db.create_task(
            id="test-project/preflight-valid",
            project_id="test-project",
            goal="Test pre-flight valid",
        )
        await db.update_task(task["id"], status="working")

        mock_validate = AsyncMock(return_value={
            "status": "validated",
            "message": "Credential validated (as octocat)",
            "checked_at": "2026-01-01T00:00:00Z",
        })

        with patch("switchboard.git.validation.validate_project_access", mock_validate):
            await _dispatch_launch_session(task)

        # Task should not be held — pre-flight passed
        updated = await db.get_task("test-project/preflight-valid")
        assert updated["status"] != "held"

    async def test_retry_holds_task_on_missing_credential(self, db, sample_project):
        """Retry path also gates on credential validation."""
        from switchboard.dispatch.lifecycle import _retry_launch_session

        task = await db.create_task(
            id="test-project/retry-preflight",
            project_id="test-project",
            goal="Test retry pre-flight",
        )
        await db.update_task(task["id"], status="working")

        mock_validate = AsyncMock(return_value={
            "status": "warning",
            "message": "No credential configured.",
            "checked_at": "2026-01-01T00:00:00Z",
        })

        with patch("switchboard.git.validation.validate_project_access", mock_validate):
            await _retry_launch_session(task)

        updated = await db.get_task("test-project/retry-preflight")
        assert updated["status"] == "held"


# ── Layer 1: Settings test endpoint ───────────────────────────────────────


def _make_scope(method="POST", path="/", user=None):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [],
        "session_user": user or {"id": 1, "email": "owner@localhost", "name": "Owner", "role": "owner"},
    }


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


class TestSettingsTestEndpoint:
    """Settings test endpoint returns ok, username, scopes, message."""

    async def test_github_classic_pat_with_repo_scope(self, db):
        """GitHub classic PAT with repo scope → ok=True, scopes includes 'repo'."""
        from switchboard.dashboard.api import _handle_test_git_credential

        await db.create_credential("github", "ghp_test_token", "github.com")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"login": "octocat"}
        mock_resp.headers = {"X-OAuth-Scopes": "repo, read:org"}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        send = _Capture()
        scope = _make_scope()

        with patch("switchboard.dashboard.api.httpx.AsyncClient", return_value=mock_client):
            await _handle_test_git_credential(send, scope, "github")

        body = send.json()
        assert body["ok"] is True
        assert body["username"] == "octocat"
        assert "repo" in body["scopes"]
        assert "Required scopes present" in body["message"]

    async def test_github_classic_pat_missing_repo_scope(self, db):
        """GitHub classic PAT without repo scope → ok=False."""
        from switchboard.dashboard.api import _handle_test_git_credential

        await db.create_credential("github", "ghp_readonly", "github.com")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"login": "octocat"}
        mock_resp.headers = {"X-OAuth-Scopes": "read:org"}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        send = _Capture()
        scope = _make_scope()

        with patch("switchboard.dashboard.api.httpx.AsyncClient", return_value=mock_client):
            await _handle_test_git_credential(send, scope, "github")

        body = send.json()
        assert body["ok"] is False
        assert "missing" in body["message"].lower()

    async def test_github_fine_grained_token(self, db):
        """GitHub fine-grained token (no X-OAuth-Scopes header) → ok=True, scopes=None."""
        from switchboard.dashboard.api import _handle_test_git_credential

        await db.create_credential("github", "github_pat_fine_grained", "github.com")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"login": "octocat"}
        mock_resp.headers = {}  # No X-OAuth-Scopes

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        send = _Capture()
        scope = _make_scope()

        with patch("switchboard.dashboard.api.httpx.AsyncClient", return_value=mock_client):
            await _handle_test_git_credential(send, scope, "github")

        body = send.json()
        assert body["ok"] is True
        assert body["scopes"] is None
        assert "fine-grained" in body["message"].lower()

    async def test_gitlab_with_api_scope(self, db):
        """GitLab token with api scope → ok=True."""
        from switchboard.dashboard.api import _handle_test_git_credential

        await db.create_credential("gitlab", "glpat_test", "gitlab.com")

        user_resp = MagicMock()
        user_resp.status_code = 200
        user_resp.json.return_value = {"username": "gluser"}

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"scopes": ["api"]}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[user_resp, token_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        send = _Capture()
        scope = _make_scope()

        with patch("switchboard.dashboard.api.httpx.AsyncClient", return_value=mock_client):
            await _handle_test_git_credential(send, scope, "gitlab")

        body = send.json()
        assert body["ok"] is True
        assert "api" in body["scopes"]

    async def test_gitlab_missing_scopes(self, db):
        """GitLab token without sufficient scopes → ok=False."""
        from switchboard.dashboard.api import _handle_test_git_credential

        await db.create_credential("gitlab", "glpat_readonly", "gitlab.com")

        user_resp = MagicMock()
        user_resp.status_code = 200
        user_resp.json.return_value = {"username": "gluser"}

        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {"scopes": ["read_user"]}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=[user_resp, token_resp])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        send = _Capture()
        scope = _make_scope()

        with patch("switchboard.dashboard.api.httpx.AsyncClient", return_value=mock_client):
            await _handle_test_git_credential(send, scope, "gitlab")

        body = send.json()
        assert body["ok"] is False
        assert "missing required scopes" in body["message"]
        assert "api" in body["message"]

    async def test_bitbucket_auth_success(self, db):
        """Bitbucket auth success → ok=True with scope note."""
        from switchboard.dashboard.api import _handle_test_git_credential

        await db.create_credential("bitbucket", "bbuser:app_pwd", "bitbucket.org")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"username": "bbuser"}

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        send = _Capture()
        scope = _make_scope()

        with patch("switchboard.dashboard.api.httpx.AsyncClient", return_value=mock_client):
            await _handle_test_git_credential(send, scope, "bitbucket")

        body = send.json()
        assert body["ok"] is True
        assert body["scopes"] is None
        assert "introspection" in body["message"].lower()

    async def test_no_credential_configured(self, db):
        """No credential configured → ok=False with message."""
        from switchboard.dashboard.api import _handle_test_git_credential

        send = _Capture()
        scope = _make_scope()

        await _handle_test_git_credential(send, scope, "github")

        body = send.json()
        assert body["ok"] is False
        assert "no" in body["message"].lower() and "configured" in body["message"].lower()

    async def test_response_shape(self, db):
        """Response always includes ok, username, scopes, message keys."""
        from switchboard.dashboard.api import _handle_test_git_credential

        send = _Capture()
        scope = _make_scope()

        await _handle_test_git_credential(send, scope, "github")

        body = send.json()
        assert "ok" in body
        assert "username" in body
        assert "scopes" in body
        assert "message" in body


# ── Dead code removal verification ───────────────────────────────────────


class TestDeadCodeRemoved:
    """Verify legacy GitHub-only functions are removed from operations.py."""

    def test_build_authenticated_url_removed(self):
        import switchboard.git.operations as ops
        assert not hasattr(ops, "_build_authenticated_url"), \
            "_build_authenticated_url should be removed — use provider.build_authenticated_url()"

    def test_create_github_pr_removed(self):
        import switchboard.git.operations as ops
        assert not hasattr(ops, "create_github_pr"), \
            "create_github_pr should be removed — use provider.create_pr()"

    def test_find_existing_pr_removed(self):
        import switchboard.git.operations as ops
        assert not hasattr(ops, "_find_existing_pr"), \
            "_find_existing_pr should be removed — use provider._find_existing_pr()"

    def test_parse_repo_url_still_exists(self):
        """parse_repo_url and normalize_repo_url should still exist (used by other code)."""
        from switchboard.git.operations import parse_repo_url, normalize_repo_url
        assert callable(parse_repo_url)
        assert callable(normalize_repo_url)


# ── normalize_repo_url passthrough for non-GitHub ─────────────────────────


class TestNormalizeRepoUrlPassthrough:
    """normalize_repo_url must not break on non-GitHub URLs."""

    def test_gitlab_https_preserved(self):
        from switchboard.git.operations import normalize_repo_url
        url = "https://gitlab.com/acme/project.git"
        assert normalize_repo_url(url) == url

    def test_bitbucket_https_preserved(self):
        from switchboard.git.operations import normalize_repo_url
        url = "https://bitbucket.org/workspace/repo.git"
        assert normalize_repo_url(url) == url

    def test_github_url_still_normalized(self):
        from switchboard.git.operations import normalize_repo_url
        assert normalize_repo_url("git@github.com:acme/widgets") == "https://github.com/acme/widgets.git"

    def test_ssh_gitlab_normalized_to_https(self):
        from switchboard.git.operations import normalize_repo_url
        assert normalize_repo_url("git@gitlab.com:group/project.git") == "https://gitlab.com/group/project.git"

    def test_self_hosted_ssh_normalized(self):
        from switchboard.git.operations import normalize_repo_url
        assert normalize_repo_url("git@gl.example.com:team/app") == "https://gl.example.com/team/app.git"
