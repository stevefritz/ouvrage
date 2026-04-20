"""Tests for PR status sweep and gh CLI guard."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from ouvrage.dispatch.pr_sweep import (
    _parse_pr_url,
    _check_pr_status,
    _handle_pr_merged,
    _pr_status_sweep,
)
from ouvrage.git.providers.base import RepoInfo


# ---------------------------------------------------------------------------
# _parse_pr_url
# ---------------------------------------------------------------------------

class TestParsePrUrl:
    def test_standard_url(self):
        owner, repo, number = _parse_pr_url("https://github.com/acme/widgets/pull/42")
        assert owner == "acme"
        assert repo == "widgets"
        assert number == 42

    def test_http_scheme(self):
        owner, repo, number = _parse_pr_url("http://github.com/acme/widgets/pull/7")
        assert owner == "acme"
        assert repo == "widgets"
        assert number == 7

    def test_org_with_hyphens(self):
        owner, repo, number = _parse_pr_url("https://github.com/my-org/my-repo/pull/100")
        assert owner == "my-org"
        assert repo == "my-repo"
        assert number == 100

    def test_trailing_whitespace_stripped(self):
        owner, repo, number = _parse_pr_url("  https://github.com/acme/widgets/pull/5  ")
        assert owner == "acme"
        assert number == 5

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse PR URL"):
            _parse_pr_url("https://gitlab.com/acme/widgets/pull/1")

    def test_missing_pull_raises(self):
        with pytest.raises(ValueError, match="Cannot parse PR URL"):
            _parse_pr_url("https://github.com/acme/widgets")

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="Cannot parse PR URL"):
            _parse_pr_url("")


# ---------------------------------------------------------------------------
# _check_pr_status
# ---------------------------------------------------------------------------

def _make_mock_provider(status_dict: dict):
    """Build a mock provider + credential pair for patching resolve_credential."""
    mock_provider = MagicMock()
    mock_provider.parse_pr_url = MagicMock(
        return_value=(RepoInfo(owner="acme", repo="widgets", hostname="github.com"), 42)
    )
    mock_provider.get_pr_status = AsyncMock(return_value=status_dict)
    return mock_provider, "ghp_test"


def _patch_resolve_credential(status_dict: dict, project_dict: dict | None = None):
    """Patch resolve_credential and db.get_project for pr_sweep tests."""
    mock_provider, credential = _make_mock_provider(status_dict)
    project = project_dict or {"id": "test-proj", "repo": "https://github.com/acme/widgets.git"}
    ctx = patch("ouvrage.dispatch.pr_sweep.resolve_credential",
                AsyncMock(return_value=(mock_provider, credential)))
    db_ctx = patch("ouvrage.dispatch.pr_sweep.db.get_project",
                   AsyncMock(return_value=project))
    return ctx, db_ctx, mock_provider


class TestCheckPrStatus:
    @pytest.mark.asyncio
    async def test_open_pr(self):
        ctx, db_ctx, _ = _patch_resolve_credential({"state": "open", "merged": False})
        with ctx, db_ctx:
            status = await _check_pr_status("https://github.com/acme/widgets/pull/1", "test-proj")
        assert status == "open"

    @pytest.mark.asyncio
    async def test_merged_pr(self):
        ctx, db_ctx, _ = _patch_resolve_credential({"state": "closed", "merged": True})
        with ctx, db_ctx:
            status = await _check_pr_status("https://github.com/acme/widgets/pull/2", "test-proj")
        assert status == "merged"

    @pytest.mark.asyncio
    async def test_closed_unmerged_pr(self):
        ctx, db_ctx, _ = _patch_resolve_credential({"state": "closed", "merged": False})
        with ctx, db_ctx:
            status = await _check_pr_status("https://github.com/acme/widgets/pull/3", "test-proj")
        assert status == "closed"

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        mock_provider = MagicMock()
        mock_provider.parse_pr_url = MagicMock(
            return_value=(RepoInfo(owner="acme", repo="widgets", hostname="github.com"), 99)
        )
        mock_provider.get_pr_status = AsyncMock(side_effect=Exception("404 Not Found"))
        project = {"id": "test-proj", "repo": "https://github.com/acme/widgets.git"}
        with patch("ouvrage.dispatch.pr_sweep.resolve_credential",
                   AsyncMock(return_value=(mock_provider, "ghp_test"))):
            with patch("ouvrage.dispatch.pr_sweep.db.get_project",
                       AsyncMock(return_value=project)):
                with pytest.raises(Exception, match="404"):
                    await _check_pr_status("https://github.com/acme/widgets/pull/99", "test-proj")

    @pytest.mark.asyncio
    async def test_uses_provider_get_pr_status(self):
        """_check_pr_status calls provider.get_pr_status with correct args."""
        ctx, db_ctx, mock_provider = _patch_resolve_credential({"state": "open", "merged": False})
        mock_provider.parse_pr_url = MagicMock(
            return_value=(RepoInfo(owner="myorg", repo="myrepo", hostname="github.com"), 77)
        )
        with ctx, db_ctx:
            await _check_pr_status("https://github.com/myorg/myrepo/pull/77", "test-proj")

        mock_provider.get_pr_status.assert_called_once()
        call_args = mock_provider.get_pr_status.call_args
        assert call_args[0][1].owner == "myorg"
        assert call_args[0][1].repo == "myrepo"
        assert call_args[0][2] == 77


# ---------------------------------------------------------------------------
# _handle_pr_merged
# ---------------------------------------------------------------------------

class TestHandlePrMerged:
    @pytest.mark.asyncio
    async def test_posts_merged_message(self, db, sample_task):
        task = dict(sample_task)
        task["pr_url"] = "https://github.com/acme/widgets/pull/42"
        await db.add_artifact(task["id"], type="pr_url", ref=task["pr_url"])

        await _handle_pr_merged(task)

        thread = await db.read_task_messages(task["id"])
        msgs = thread.get("messages", [])
        merged_msgs = [m for m in msgs if m.get("title") == "PR merged"]
        assert len(merged_msgs) == 1
        assert "#42" in merged_msgs[0]["content"]
        assert "✅" in merged_msgs[0]["content"]
        assert merged_msgs[0]["author"] == "dispatcher"
        assert merged_msgs[0]["type"] == "status"

    @pytest.mark.asyncio
    async def test_transitions_completed_passed_to_merged(self, db, sample_task):
        task = dict(sample_task)
        task["pr_url"] = "https://github.com/acme/widgets/pull/5"
        # Set status to completed + gate_status to passed
        await db.update_task(task["id"], status="completed", gate_status="passed")
        task["status"] = "completed"
        task["gate_status"] = "passed"

        await _handle_pr_merged(task)

        updated = await db.get_task(task["id"])
        assert updated["status"] == "merged"

    @pytest.mark.asyncio
    async def test_no_transition_if_not_completed(self, db, sample_task):
        task = dict(sample_task)
        task["pr_url"] = "https://github.com/acme/widgets/pull/5"
        task["status"] = "working"
        task["gate_status"] = "passed"

        await _handle_pr_merged(task)

        updated = await db.get_task(task["id"])
        # should still be working, not merged
        assert updated["status"] == "working"

    @pytest.mark.asyncio
    async def test_no_transition_if_gate_not_passed(self, db, sample_task):
        task = dict(sample_task)
        task["pr_url"] = "https://github.com/acme/widgets/pull/5"
        await db.update_task(task["id"], status="completed")
        task["status"] = "completed"
        task["gate_status"] = "testing"

        await _handle_pr_merged(task)

        updated = await db.get_task(task["id"])
        assert updated["status"] == "completed"

    @pytest.mark.asyncio
    async def test_message_without_pr_number_on_bad_url(self, db, sample_task):
        """Handles tasks where pr_url can't be parsed (no pr_number in message)."""
        task = dict(sample_task)
        task["pr_url"] = "https://github.com/acme/widgets"  # no /pull/N
        task["status"] = "completed"
        task["gate_status"] = "passed"

        await _handle_pr_merged(task)

        thread = await db.read_task_messages(task["id"])
        msgs = [m for m in thread.get("messages", []) if m.get("title") == "PR merged"]
        assert msgs  # message still posted


# ---------------------------------------------------------------------------
# _pr_status_sweep loop
# ---------------------------------------------------------------------------

class TestPrStatusSweep:
    @pytest.mark.asyncio
    async def test_sweep_updates_pr_status(self, db, sample_task):
        """When GitHub returns 'merged', the task pr_status is updated."""
        # Add pr_url artifact to task
        pr_url = "https://github.com/acme/widgets/pull/10"
        await db.add_artifact(sample_task["id"], type="pr_url", ref=pr_url)

        tasks_list = [dict(sample_task)]
        tasks_list[0]["pr_url"] = pr_url
        tasks_list[0]["pr_status"] = None

        # sleep side_effect: first call passes (None), second raises to stop loop
        with patch("ouvrage.dispatch.pr_sweep.db.get_tasks_with_open_prs", AsyncMock(return_value=tasks_list)):
            with patch("ouvrage.dispatch.pr_sweep._check_pr_status", AsyncMock(return_value="merged")):
                with patch("ouvrage.dispatch.pr_sweep.db.update_task", AsyncMock()) as mock_update:
                    with patch("ouvrage.dispatch.pr_sweep._handle_pr_merged", AsyncMock()) as mock_handle:
                        with patch("asyncio.sleep", AsyncMock(side_effect=[None, StopAsyncIteration])):
                            try:
                                await _pr_status_sweep()
                            except StopAsyncIteration:
                                pass

        mock_update.assert_called_once_with(sample_task["id"], pr_status="merged")
        mock_handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_sweep_skips_unchanged_status(self, db, sample_task):
        """When pr_status hasn't changed, no update is written."""
        pr_url = "https://github.com/acme/widgets/pull/11"
        tasks_list = [dict(sample_task)]
        tasks_list[0]["pr_url"] = pr_url
        tasks_list[0]["pr_status"] = "open"

        with patch("ouvrage.dispatch.pr_sweep.db.get_tasks_with_open_prs", AsyncMock(return_value=tasks_list)):
            with patch("ouvrage.dispatch.pr_sweep._check_pr_status", AsyncMock(return_value="open")):
                with patch("ouvrage.dispatch.pr_sweep.db.update_task", AsyncMock()) as mock_update:
                    with patch("asyncio.sleep", AsyncMock(side_effect=[None, StopAsyncIteration])):
                        try:
                            await _pr_status_sweep()
                        except StopAsyncIteration:
                            pass

        mock_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweep_handles_task_exception_gracefully(self):
        """An exception for one task shouldn't stop the sweep."""
        tasks_list = [
            {"id": "proj/task-a", "pr_url": "https://github.com/a/b/pull/1",
             "pr_status": None, "project_id": "proj"},
            {"id": "proj/task-b", "pr_url": "https://github.com/a/b/pull/2",
             "pr_status": None, "project_id": "proj"},
        ]

        call_count = 0

        async def _flaky_check(pr_url, project_id, user_id=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("API error")
            return "open"

        with patch("ouvrage.dispatch.pr_sweep.db.get_tasks_with_open_prs", AsyncMock(return_value=tasks_list)):
            with patch("ouvrage.dispatch.pr_sweep._check_pr_status", side_effect=_flaky_check):
                with patch("ouvrage.dispatch.pr_sweep.db.update_task", AsyncMock()):
                    with patch("asyncio.sleep", AsyncMock(side_effect=[None, StopAsyncIteration])):
                        try:
                            await _pr_status_sweep()
                        except StopAsyncIteration:
                            pass

        # Both tasks were attempted — error in task-a didn't abort task-b
        assert call_count == 2


# ---------------------------------------------------------------------------
# DB: get_tasks_with_open_prs
# ---------------------------------------------------------------------------

class TestGetTasksWithOpenPrs:
    @pytest.mark.asyncio
    async def test_returns_tasks_with_pr_url_and_null_status(self, db, sample_task):
        pr_url = "https://github.com/acme/widgets/pull/99"
        await db.add_artifact(sample_task["id"], type="pr_url", ref=pr_url)

        tasks = await db.get_tasks_with_open_prs()
        assert any(t["id"] == sample_task["id"] for t in tasks)
        found = next(t for t in tasks if t["id"] == sample_task["id"])
        assert found["pr_url"] == pr_url

    @pytest.mark.asyncio
    async def test_excludes_merged_tasks(self, db, sample_task):
        await db.add_artifact(sample_task["id"], type="pr_url", ref="https://github.com/a/b/pull/1")
        await db.update_task(sample_task["id"], pr_status="merged")

        tasks = await db.get_tasks_with_open_prs()
        assert not any(t["id"] == sample_task["id"] for t in tasks)

    @pytest.mark.asyncio
    async def test_excludes_closed_tasks(self, db, sample_task):
        await db.add_artifact(sample_task["id"], type="pr_url", ref="https://github.com/a/b/pull/2")
        await db.update_task(sample_task["id"], pr_status="closed")

        tasks = await db.get_tasks_with_open_prs()
        assert not any(t["id"] == sample_task["id"] for t in tasks)

    @pytest.mark.asyncio
    async def test_includes_open_status(self, db, sample_task):
        await db.add_artifact(sample_task["id"], type="pr_url", ref="https://github.com/a/b/pull/3")
        await db.update_task(sample_task["id"], pr_status="open")

        tasks = await db.get_tasks_with_open_prs()
        assert any(t["id"] == sample_task["id"] for t in tasks)

    @pytest.mark.asyncio
    async def test_excludes_tasks_without_pr_url(self, db, sample_task):
        # sample_task has no pr_url artifact
        tasks = await db.get_tasks_with_open_prs()
        assert not any(t["id"] == sample_task["id"] for t in tasks)


# ---------------------------------------------------------------------------
# _gh_cli_guard (can_use_tool hook)
# ---------------------------------------------------------------------------

class TestGhCliGuard:
    @pytest.mark.asyncio
    async def test_allows_non_bash(self):
        from ouvrage.dispatch.sdk_session import _gh_cli_guard
        from claude_agent_sdk import PermissionResultAllow
        from claude_agent_sdk.types import ToolPermissionContext

        ctx = ToolPermissionContext()
        result = await _gh_cli_guard("Read", {"file_path": "/foo"}, ctx)
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_blocks_gh_pr_create(self):
        from ouvrage.dispatch.sdk_session import _gh_cli_guard
        from claude_agent_sdk import PermissionResultDeny
        from claude_agent_sdk.types import ToolPermissionContext

        ctx = ToolPermissionContext()
        result = await _gh_cli_guard("Bash", {"command": "gh pr create --title 'Test'"}, ctx)
        assert isinstance(result, PermissionResultDeny)
        assert "gh CLI is not allowed" in result.message

    @pytest.mark.asyncio
    async def test_blocks_gh_in_middle_of_command(self):
        from ouvrage.dispatch.sdk_session import _gh_cli_guard
        from claude_agent_sdk import PermissionResultDeny
        from claude_agent_sdk.types import ToolPermissionContext

        ctx = ToolPermissionContext()
        result = await _gh_cli_guard("Bash", {"command": "echo test && gh pr list"}, ctx)
        assert isinstance(result, PermissionResultDeny)

    @pytest.mark.asyncio
    async def test_allows_regular_bash(self):
        from ouvrage.dispatch.sdk_session import _gh_cli_guard
        from claude_agent_sdk import PermissionResultAllow
        from claude_agent_sdk.types import ToolPermissionContext

        ctx = ToolPermissionContext()
        result = await _gh_cli_guard("Bash", {"command": "git status"}, ctx)
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_allows_bash_without_gh(self):
        from ouvrage.dispatch.sdk_session import _gh_cli_guard
        from claude_agent_sdk import PermissionResultAllow
        from claude_agent_sdk.types import ToolPermissionContext

        ctx = ToolPermissionContext()
        result = await _gh_cli_guard("Bash", {"command": "python3 -m pytest tests/"}, ctx)
        assert isinstance(result, PermissionResultAllow)

    @pytest.mark.asyncio
    async def test_blocks_piped_gh(self):
        from ouvrage.dispatch.sdk_session import _gh_cli_guard
        from claude_agent_sdk import PermissionResultDeny
        from claude_agent_sdk.types import ToolPermissionContext

        ctx = ToolPermissionContext()
        result = await _gh_cli_guard("Bash", {"command": "cat file.txt|gh pr create"}, ctx)
        assert isinstance(result, PermissionResultDeny)

    @pytest.mark.asyncio
    async def test_rejection_message_mentions_gate_pipeline(self):
        from ouvrage.dispatch.sdk_session import _gh_cli_guard
        from claude_agent_sdk import PermissionResultDeny
        from claude_agent_sdk.types import ToolPermissionContext

        ctx = ToolPermissionContext()
        result = await _gh_cli_guard("Bash", {"command": "gh pr create"}, ctx)
        assert isinstance(result, PermissionResultDeny)
        assert "gate pipeline" in result.message
