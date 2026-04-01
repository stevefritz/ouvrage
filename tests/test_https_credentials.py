"""Tests for HTTPS URL normalization and credential helper setup."""

import os
import stat
import pytest
from unittest.mock import AsyncMock, patch, MagicMock, call

from switchboard.git.operations import normalize_repo_url, parse_repo_url


# ---------------------------------------------------------------------------
# normalize_repo_url
# ---------------------------------------------------------------------------

class TestNormalizeRepoUrl:
    """normalize_repo_url must always return canonical https://github.com/owner/repo.git"""

    def test_ssh_with_git_suffix(self):
        assert normalize_repo_url("git@github.com:acme/widgets.git") == "https://github.com/acme/widgets.git"

    def test_ssh_without_git_suffix(self):
        assert normalize_repo_url("git@github.com:acme/widgets") == "https://github.com/acme/widgets.git"

    def test_https_passthrough_with_suffix(self):
        assert normalize_repo_url("https://github.com/acme/widgets.git") == "https://github.com/acme/widgets.git"

    def test_https_passthrough_without_suffix(self):
        assert normalize_repo_url("https://github.com/acme/widgets") == "https://github.com/acme/widgets.git"

    def test_http_scheme(self):
        assert normalize_repo_url("http://github.com/acme/widgets") == "https://github.com/acme/widgets.git"

    def test_hyphens_preserved(self):
        assert normalize_repo_url("git@github.com:my-org/my-repo.git") == "https://github.com/my-org/my-repo.git"

    def test_dots_in_repo_name(self):
        assert normalize_repo_url("git@github.com:org/repo.name.git") == "https://github.com/org/repo.name.git"

    def test_result_always_has_git_suffix(self):
        result = normalize_repo_url("git@github.com:org/repo")
        assert result.endswith(".git")

    def test_result_always_starts_with_https(self):
        result = normalize_repo_url("git@github.com:org/repo.git")
        assert result.startswith("https://")

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            normalize_repo_url("not-a-url")

    def test_gitlab_url_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            normalize_repo_url("git@gitlab.com:acme/widgets.git")


# ---------------------------------------------------------------------------
# Handler: create_project normalizes repo URL
# ---------------------------------------------------------------------------

class TestCreateProjectNormalizesUrl:
    """_handle_create_project must store HTTPS URL even if SSH is passed."""

    @pytest.fixture(autouse=True)
    def _patches(self):
        self.created_args = {}

        async def mock_create_project(**kwargs):
            self.created_args = kwargs
            return kwargs

        self.patches = [
            patch("switchboard.server.handlers.projects.db.create_project", side_effect=mock_create_project),
            patch("switchboard.server.handlers.projects.db.list_projects", AsyncMock(return_value=[])),
            patch("switchboard.server.handlers.projects.db.get_max_projects", AsyncMock(return_value=0)),
            patch("switchboard.server.handlers.projects.get_request_user_id", return_value=1),
            patch("os.path.realpath", side_effect=lambda p: p),
            patch("switchboard.server.handlers.projects._validate_github_pat_for_repo", AsyncMock(return_value=None)),
            patch("switchboard.server.handlers.projects.SKIP_CREDENTIAL_CHECK", False),
            patch("switchboard.server.handlers.projects.WORKTREE_BASE", "/work"),
        ]
        for p in self.patches:
            p.start()
        yield
        for p in self.patches:
            p.stop()

    _REQUIRED_CONFIG = {
        "model": "sonnet",
        "review_model": "sonnet",
        "auto_test": True,
        "auto_review": True,
        "auto_pr": False,
        "auto_merge": False,
        "max_turns": 50,
        "max_wall_clock": 3600,
    }

    @pytest.mark.asyncio
    async def test_ssh_url_normalized_to_https(self):
        from switchboard.server.handlers.projects import _handle_create_project
        await _handle_create_project({
            "id": "test-proj",
            "repo": "git@github.com:acme/widgets.git",
            "working_dir": "/work/widgets",
            **self._REQUIRED_CONFIG,
        })
        assert self.created_args["repo"] == "https://github.com/acme/widgets.git"

    @pytest.mark.asyncio
    async def test_https_url_passthrough(self):
        from switchboard.server.handlers.projects import _handle_create_project
        await _handle_create_project({
            "id": "test-proj",
            "repo": "https://github.com/acme/widgets.git",
            "working_dir": "/work/widgets",
            **self._REQUIRED_CONFIG,
        })
        assert self.created_args["repo"] == "https://github.com/acme/widgets.git"


# ---------------------------------------------------------------------------
# Handler: update_project normalizes repo URL
# ---------------------------------------------------------------------------

class TestUpdateProjectNormalizesUrl:
    """_handle_update_project must normalize repo URL if provided."""

    @pytest.fixture(autouse=True)
    def _patches(self):
        self.updated_id = None
        self.updated_fields = {}

        async def mock_update_project(project_id, **fields):
            self.updated_id = project_id
            self.updated_fields = fields
            return {"id": project_id, **fields}

        self.patches = [
            patch("switchboard.server.handlers.projects.db.update_project", side_effect=mock_update_project),
        ]
        for p in self.patches:
            p.start()
        yield
        for p in self.patches:
            p.stop()

    @pytest.mark.asyncio
    async def test_ssh_repo_normalized_on_update(self):
        from switchboard.server.handlers.projects import _handle_update_project
        await _handle_update_project({
            "id": "test-proj",
            "repo": "git@github.com:acme/widgets.git",
        })
        assert self.updated_fields["repo"] == "https://github.com/acme/widgets.git"

    @pytest.mark.asyncio
    async def test_no_repo_field_not_normalized(self):
        from switchboard.server.handlers.projects import _handle_update_project
        await _handle_update_project({
            "id": "test-proj",
            "test_command": "pytest",
        })
        assert "repo" not in self.updated_fields

    @pytest.mark.asyncio
    async def test_https_repo_unchanged_on_update(self):
        from switchboard.server.handlers.projects import _handle_update_project
        await _handle_update_project({
            "id": "test-proj",
            "repo": "https://github.com/acme/widgets",
        })
        assert self.updated_fields["repo"] == "https://github.com/acme/widgets.git"


# ---------------------------------------------------------------------------
# setup_credential_helper
# ---------------------------------------------------------------------------

class TestSetupCredentialHelper:
    """Credential helper must be written, chmod'd, and git config set."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.worktree = str(tmp_path / "worktree")
        os.makedirs(self.worktree)
        self.project_id = "test-proj"
        self.pat = "ghp_test_token_12345"

        # side_effect that simulates file creation for bash heredoc + chmod
        async def _simulate_run(*args, **kwargs):
            cmd_str = " ".join(str(a) for a in args)
            if args[0] == "bash" and args[1] == "-c" and "cat >" in args[2]:
                # Extract path and content from the heredoc command
                import re
                m = re.search(r"cat > (\S+) << 'CREDEOF'\n(.*?)CREDEOF", args[2], re.DOTALL)
                if m:
                    path, content = m.group(1), m.group(2)
                    with open(path, "w") as f:
                        f.write(content)
            elif args[0] == "chmod" and args[1] == "700":
                os.chmod(args[2], 0o700)
            return (b"", b"", 0)

        self.mock_run = AsyncMock(side_effect=_simulate_run)
        self.mock_get_pat = AsyncMock(return_value=self.pat)
        self.mock_get_project = AsyncMock(return_value={
            "id": self.project_id,
            "repo": "https://github.com/acme/widgets.git",
        })
        self.mock_get_worker_ids = MagicMock(return_value=(1000, 1000))

        self.patches = [
            patch("switchboard.git.worktree._run_as_worker", self.mock_run),
            patch("switchboard.git.worktree.db.get_project", self.mock_get_project),
            patch("switchboard.git.worktree._get_worker_ids", self.mock_get_worker_ids),
            patch("switchboard.git.worktree.os.chown", MagicMock()),
        ]
        for p in self.patches:
            p.start()
        yield
        for p in self.patches:
            p.stop()

    @pytest.mark.asyncio
    async def test_helper_path_returned(self):
        from switchboard.git.worktree import setup_credential_helper
        with patch("switchboard.git.worktree.get_github_pat", self.mock_get_pat):
            result = await setup_credential_helper(self.worktree, self.project_id)
        assert result is not None
        assert result.startswith(self.worktree)
        assert result.endswith(".git-credential-helper.sh")

    @pytest.mark.asyncio
    async def test_helper_file_written(self):
        """Helper script is written to worktree with correct content."""
        from switchboard.git.worktree import setup_credential_helper
        with patch("switchboard.git.worktree.get_github_pat", self.mock_get_pat):
            result = await setup_credential_helper(self.worktree, self.project_id)
        assert os.path.exists(result)
        content = open(result).read()
        assert "username=oauth2" in content
        assert self.pat in content

    @pytest.mark.asyncio
    async def test_helper_file_executable(self):
        from switchboard.git.worktree import setup_credential_helper
        with patch("switchboard.git.worktree.get_github_pat", self.mock_get_pat):
            result = await setup_credential_helper(self.worktree, self.project_id)
        mode = os.stat(result).st_mode
        assert mode & 0o700 == 0o700

    @pytest.mark.asyncio
    async def test_credential_helper_worktree_scoped(self):
        """credential.helper must use --worktree scope to avoid bare repo pollution."""
        from switchboard.git.worktree import setup_credential_helper
        with patch("switchboard.git.worktree.get_github_pat", self.mock_get_pat):
            await setup_credential_helper(self.worktree, self.project_id)
        config_calls = [
            c for c in self.mock_run.call_args_list
            if "config" in c.args and "credential.helper" in c.args
        ]
        assert len(config_calls) == 1
        assert "--worktree" in config_calls[0].args

    @pytest.mark.asyncio
    async def test_remote_url_worktree_scoped(self):
        """remote.origin.url must use --worktree scope."""
        from switchboard.git.worktree import setup_credential_helper
        with patch("switchboard.git.worktree.get_github_pat", self.mock_get_pat):
            await setup_credential_helper(self.worktree, self.project_id)
        remote_calls = [
            c for c in self.mock_run.call_args_list
            if "config" in c.args and "remote.origin.url" in c.args
        ]
        assert len(remote_calls) == 1
        assert "--worktree" in remote_calls[0].args
        assert "https://github.com/acme/widgets.git" in remote_calls[0].args

    @pytest.mark.asyncio
    async def test_worktree_config_extension_enabled(self):
        """extensions.worktreeConfig must be set on bare repo."""
        from switchboard.git.worktree import setup_credential_helper
        with patch("switchboard.git.worktree.get_github_pat", self.mock_get_pat):
            await setup_credential_helper(self.worktree, self.project_id)
        ext_calls = [
            c for c in self.mock_run.call_args_list
            if "extensions.worktreeConfig" in c.args
        ]
        assert len(ext_calls) == 1

    @pytest.mark.asyncio
    async def test_no_pat_returns_none(self):
        """When no PAT is configured, skip silently and return None."""
        from switchboard.git.worktree import setup_credential_helper
        mock_no_pat = AsyncMock(side_effect=ValueError("No GitHub PAT configured"))
        with patch("switchboard.git.worktree.get_github_pat", mock_no_pat):
            result = await setup_credential_helper(self.worktree, self.project_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_pat_no_git_commands(self):
        """When no PAT, no git commands should run."""
        from switchboard.git.worktree import setup_credential_helper
        mock_no_pat = AsyncMock(side_effect=ValueError("No GitHub PAT configured"))
        with patch("switchboard.git.worktree.get_github_pat", mock_no_pat):
            await setup_credential_helper(self.worktree, self.project_id)
        self.mock_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pat_not_in_git_config_commands(self):
        """PAT must NOT appear in git config calls (only in bash write)."""
        from switchboard.git.worktree import setup_credential_helper
        with patch("switchboard.git.worktree.get_github_pat", self.mock_get_pat):
            await setup_credential_helper(self.worktree, self.project_id)
        for c in self.mock_run.call_args_list:
            if c.args[0] == "git":
                for arg in c.args:
                    assert self.pat not in str(arg), f"PAT found in git command: {c.args}"


# ---------------------------------------------------------------------------
# Startup migration — SSH URLs in DB converted to HTTPS
# ---------------------------------------------------------------------------

class TestStartupMigration:
    """init_db migration must convert SSH repo URLs to HTTPS."""

    @pytest.mark.asyncio
    async def test_ssh_url_migrated(self, db):
        """Projects with SSH URLs should be migrated to HTTPS on init_db."""
        # Insert a project with SSH URL directly (bypassing the handler)
        async with db.get_db() as conn:
            await conn.execute(
                "INSERT INTO projects (id, repo, default_branch, working_dir, created_at) VALUES (?, ?, ?, ?, ?)",
                ("ssh-proj", "git@github.com:acme/widgets.git", "main", "/work/widgets", "2024-01-01T00:00:00Z"),
            )
            await conn.commit()

        # Re-run init_db (migration runs again)
        with patch("asyncio.create_subprocess_exec") as mock_proc:
            mock_instance = MagicMock()
            mock_instance.communicate = AsyncMock(return_value=(b"", b""))
            mock_instance.returncode = 0
            mock_proc.return_value = mock_instance
            # Ensure bare path does NOT exist so we skip git remote set-url
            with patch("os.path.exists", return_value=False):
                from switchboard.db.schema import init_db
                await init_db()

        # Verify the URL was updated
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("SELECT repo FROM projects WHERE id = 'ssh-proj'")
        assert rows[0]["repo"] == "https://github.com/acme/widgets.git"

    @pytest.mark.asyncio
    async def test_https_url_unchanged(self, db):
        """Projects with HTTPS URLs should not be modified."""
        async with db.get_db() as conn:
            await conn.execute(
                "INSERT INTO projects (id, repo, default_branch, working_dir, created_at) VALUES (?, ?, ?, ?, ?)",
                ("https-proj", "https://github.com/acme/widgets.git", "main", "/work/widgets", "2024-01-01T00:00:00Z"),
            )
            await conn.commit()

        with patch("os.path.exists", return_value=False):
            from switchboard.db.schema import init_db
            await init_db()

        async with db.get_db() as conn:
            rows = await conn.execute_fetchall("SELECT repo FROM projects WHERE id = 'https-proj'")
        assert rows[0]["repo"] == "https://github.com/acme/widgets.git"

    @pytest.mark.asyncio
    async def test_bare_repo_remote_updated(self, db, tmp_path):
        """If bare repo exists, git remote set-url should be called."""
        bare_path = str(tmp_path / ".bare")
        os.makedirs(bare_path)
        working_dir = str(tmp_path)

        async with db.get_db() as conn:
            await conn.execute(
                "INSERT INTO projects (id, repo, default_branch, working_dir, created_at) VALUES (?, ?, ?, ?, ?)",
                ("bare-proj", "git@github.com:acme/widgets.git", "main", working_dir, "2024-01-01T00:00:00Z"),
            )
            await conn.commit()

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
            from switchboard.db.schema import init_db
            await init_db()

        # Verify git remote set-url was called with the HTTPS URL
        calls = mock_exec.call_args_list
        remote_set_url_calls = [
            c for c in calls
            if len(c.args) >= 4 and "remote" in c.args and "set-url" in c.args
        ]
        assert len(remote_set_url_calls) >= 1
        found = any(
            "https://github.com/acme/widgets.git" in c.args
            for c in remote_set_url_calls
        )
        assert found, "git remote set-url must be called with HTTPS URL"


# ---------------------------------------------------------------------------
# setup_worktree — bare clone uses authenticated URL
# ---------------------------------------------------------------------------

class TestBareCloneAuth:
    """setup_worktree must use authenticated URL for the initial bare clone."""

    PAT = "ghp_test_bare_clone_token"
    PROJECT_ID = "test-proj"
    REPO = "https://github.com/acme/widgets.git"
    AUTH_URL = f"https://oauth2:{PAT}@github.com/acme/widgets.git"

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        self.working_dir = str(tmp_path / "working")
        os.makedirs(self.working_dir)
        self.project = {
            "id": self.PROJECT_ID,
            "repo": self.REPO,
            "working_dir": self.working_dir,
            "default_branch": "main",
        }

        # Simulate _run_as_worker: mkdir succeeds, clone succeeds, everything else succeeds
        async def _fake_run(*args, **kwargs):
            if args[0] == "mkdir":
                os.makedirs(args[-1], exist_ok=True)
            elif args[0] == "git" and "clone" in args and "--bare" in args:
                # Create bare_path dir to simulate successful clone
                bare_path = args[-1]
                os.makedirs(bare_path, exist_ok=True)
            elif args[0] == "git" and "symbolic-ref" in args:
                return (b"refs/heads/main", b"", 0)
            elif args[0] == "git" and "worktree" in args and "add" in args:
                # Create the worktree dir
                worktree_path = args[-2] if args[-1].startswith("origin/") or args[-1] == "main" else args[-1]
                # worktree path is the second-to-last or last non-ref arg
                for a in args:
                    if str(a).startswith(str(tmp_path)) and "working" in str(a) and ".bare" not in str(a):
                        os.makedirs(str(a), exist_ok=True)
                        break
            return (b"", b"", 0)

        self.mock_run = AsyncMock(side_effect=_fake_run)
        self.mock_get_db = AsyncMock()

        self.patches = [
            patch("switchboard.git.worktree._run_as_worker", self.mock_run),
            patch("switchboard.git.worktree._get_worker_ids", MagicMock(return_value=(1000, 1000))),
        ]
        for p in self.patches:
            p.start()
        yield
        for p in self.patches:
            p.stop()

    def _get_clone_call(self):
        """Find the git clone --bare call among all _run_as_worker invocations."""
        for c in self.mock_run.call_args_list:
            if c.args[0] == "git" and "--bare" in c.args and "clone" in c.args:
                return c
        return None

    @pytest.mark.asyncio
    async def test_bare_clone_uses_authenticated_url_when_pat_available(self):
        """When PAT is configured, git clone --bare must use the authenticated URL."""
        from switchboard.git.worktree import setup_worktree

        mock_resolve = AsyncMock(return_value=self.AUTH_URL)
        with patch("switchboard.git.operations._resolve_push_url", mock_resolve):
            with patch("switchboard.git.worktree.db.get_task", AsyncMock(return_value=None)):
                try:
                    await setup_worktree(self.project, "test-task", "test-branch")
                except Exception:
                    pass  # worktree add may fail in test env — clone call is what we check

        clone_call = self._get_clone_call()
        assert clone_call is not None, "git clone --bare was not called"
        assert self.AUTH_URL in clone_call.args, (
            f"Authenticated URL not used for bare clone. Got: {clone_call.args}"
        )
        assert self.REPO not in clone_call.args, "Plain URL (no PAT) must not be used when PAT is available"

    @pytest.mark.asyncio
    async def test_bare_clone_falls_back_to_plain_url_when_no_pat(self):
        """When no PAT is configured, git clone --bare falls back to plain project URL."""
        from switchboard.git.worktree import setup_worktree

        mock_resolve = AsyncMock(side_effect=ValueError("No GitHub PAT configured"))
        with patch("switchboard.git.operations._resolve_push_url", mock_resolve):
            with patch("switchboard.git.worktree.db.get_task", AsyncMock(return_value=None)):
                try:
                    await setup_worktree(self.project, "test-task", "test-branch")
                except Exception:
                    pass

        clone_call = self._get_clone_call()
        assert clone_call is not None, "git clone --bare was not called"
        assert self.REPO in clone_call.args, (
            f"Plain project URL not used for bare clone when no PAT. Got: {clone_call.args}"
        )

    @pytest.mark.asyncio
    async def test_bare_clone_skipped_when_bare_path_exists(self):
        """When .bare already exists, git clone --bare must NOT be called."""
        from switchboard.git.worktree import setup_worktree

        # Pre-create the bare path to simulate existing project
        bare_path = os.path.join(self.working_dir, ".bare")
        os.makedirs(bare_path)

        mock_resolve = AsyncMock(return_value=self.AUTH_URL)
        with patch("switchboard.git.operations._resolve_push_url", mock_resolve):
            with patch("switchboard.git.worktree.db.get_task", AsyncMock(return_value=None)):
                try:
                    await setup_worktree(self.project, "test-task", "test-branch")
                except Exception:
                    pass

        clone_call = self._get_clone_call()
        assert clone_call is None, "git clone --bare must not run when .bare already exists"

    @pytest.mark.asyncio
    async def test_fetch_uses_authenticated_url_when_pat_available(self):
        """The post-clone git fetch must also use the authenticated URL."""
        from switchboard.git.worktree import setup_worktree

        mock_resolve = AsyncMock(return_value=self.AUTH_URL)
        with patch("switchboard.git.operations._resolve_push_url", mock_resolve):
            with patch("switchboard.git.worktree.db.get_task", AsyncMock(return_value=None)):
                try:
                    await setup_worktree(self.project, "test-task", "test-branch")
                except Exception:
                    pass

        fetch_calls = [
            c for c in self.mock_run.call_args_list
            if c.args[0] == "git" and "fetch" in c.args and "--bare" not in c.args
            and c.args[2] != self.working_dir  # not the worktree fetch at the end
        ]
        auth_fetch = any(self.AUTH_URL in c.args for c in fetch_calls)
        assert auth_fetch, "Authenticated URL must be used for the bare repo fetch"

    @pytest.mark.asyncio
    async def test_pat_stripped_from_bare_repo_remote_url_after_clone(self):
        """After bare clone, remote.origin.url must be reset to the plain URL (no PAT)."""
        from switchboard.git.worktree import setup_worktree

        mock_resolve = AsyncMock(return_value=self.AUTH_URL)
        with patch("switchboard.git.operations._resolve_push_url", mock_resolve):
            with patch("switchboard.git.worktree.db.get_task", AsyncMock(return_value=None)):
                try:
                    await setup_worktree(self.project, "test-task", "test-branch")
                except Exception:
                    pass

        # Find the git config remote.origin.url call that resets to plain URL
        reset_calls = [
            c for c in self.mock_run.call_args_list
            if c.args[0] == "git" and "config" in c.args
            and "remote.origin.url" in c.args
            and self.REPO in c.args
            and self.AUTH_URL not in c.args
        ]
        assert reset_calls, (
            "remote.origin.url must be reset to plain URL after clone to strip PAT from disk"
        )
        # And the PAT must not appear in any git config call
        for c in self.mock_run.call_args_list:
            if c.args[0] == "git" and "config" in c.args:
                for arg in c.args:
                    assert self.PAT not in str(arg), f"PAT found in git config call: {c.args}"


# ---------------------------------------------------------------------------
# setup_worktree — existing worktree fetch fallback
# ---------------------------------------------------------------------------

class TestExistingWorktreeFetchFallback:
    """setup_worktree must check fetch return code and fall back to authenticated URL on failure."""

    PAT = "ghp_test_fetch_fallback"
    PROJECT_ID = "fetch-proj"
    REPO = "https://github.com/acme/widgets.git"
    AUTH_URL = f"https://oauth2:{PAT}@github.com/acme/widgets.git"

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        # working_dir and worktree must align: worktree = os.path.join(working_dir, dir_name)
        self.working_dir = str(tmp_path / "working")
        os.makedirs(self.working_dir)
        self.dir_name = "my-task-wt"
        self.worktree_path = os.path.join(self.working_dir, self.dir_name)
        os.makedirs(self.worktree_path)  # pre-create to trigger the existing-worktree code path

        self.project = {
            "id": self.PROJECT_ID,
            "repo": self.REPO,
            "working_dir": self.working_dir,
            "default_branch": "main",
        }
        self.branch = "my-feature"

        self.mock_run = AsyncMock()
        self.patches = [
            patch("switchboard.git.worktree._run_as_worker", self.mock_run),
            patch("switchboard.git.worktree._get_worker_ids", MagicMock(return_value=(1000, 1000))),
        ]
        for p in self.patches:
            p.start()
        yield
        for p in self.patches:
            p.stop()

    @pytest.mark.asyncio
    async def test_fetch_success_no_fallback(self):
        """When fetch origin succeeds, fallback fetch is not attempted."""
        from switchboard.git.worktree import setup_worktree

        self.mock_run.return_value = (b"", b"", 0)  # all git commands succeed

        await setup_worktree(self.project, self.dir_name, self.branch)

        # There should be a 'fetch origin' call
        fetch_calls = [c for c in self.mock_run.call_args_list if "fetch" in c.args]
        assert len(fetch_calls) >= 1
        assert any("origin" in c.args for c in fetch_calls)

        # No authenticated URL should appear in any fetch call
        auth_fetches = [c for c in fetch_calls if self.AUTH_URL in c.args]
        assert len(auth_fetches) == 0

    @pytest.mark.asyncio
    async def test_fetch_failure_tries_authenticated_url(self):
        """When fetch origin fails, it retries with the authenticated URL."""
        from switchboard.git.worktree import setup_worktree

        mock_resolve = AsyncMock(return_value=self.AUTH_URL)

        async def _side_effect(*args, **kwargs):
            # Fail only fetch origin (not fetch with auth URL)
            if "fetch" in args and "origin" in args and self.AUTH_URL not in args:
                return (b"", b"network error", 1)
            return (b"", b"", 0)  # everything else (including fallback fetch) succeeds

        self.mock_run.side_effect = _side_effect

        with patch("switchboard.git.operations._resolve_push_url", mock_resolve):
            await setup_worktree(self.project, self.dir_name, self.branch)

        # Authenticated URL should appear in a fetch call
        fetch_calls = [c for c in self.mock_run.call_args_list if "fetch" in c.args]
        auth_fetches = [c for c in fetch_calls if self.AUTH_URL in c.args]
        assert len(auth_fetches) >= 1, (
            f"Expected a fetch with authenticated URL after origin fetch failure. "
            f"Fetch calls: {[c.args for c in fetch_calls]}"
        )

    @pytest.mark.asyncio
    async def test_fetch_failure_fallback_also_fails_raises(self):
        """When both fetch origin and authenticated URL fetch fail, RuntimeError is raised."""
        from switchboard.git.worktree import setup_worktree

        mock_resolve = AsyncMock(return_value=self.AUTH_URL)

        async def _side_effect(*args, **kwargs):
            if "fetch" in args:
                return (b"", b"network error", 1)  # all fetches fail
            return (b"", b"", 0)

        self.mock_run.side_effect = _side_effect

        with patch("switchboard.git.operations._resolve_push_url", mock_resolve):
            with pytest.raises(RuntimeError, match="git fetch failed"):
                await setup_worktree(self.project, self.dir_name, self.branch)

    @pytest.mark.asyncio
    async def test_fetch_failure_no_pat_raises(self):
        """When fetch fails and no PAT is available, RuntimeError is raised."""
        from switchboard.git.worktree import setup_worktree

        mock_resolve = AsyncMock(side_effect=ValueError("No GitHub PAT configured"))

        async def _side_effect(*args, **kwargs):
            if "fetch" in args and "origin" in args:
                return (b"", b"network error", 1)  # fetch origin fails
            return (b"", b"", 0)

        self.mock_run.side_effect = _side_effect

        with patch("switchboard.git.operations._resolve_push_url", mock_resolve):
            with pytest.raises(RuntimeError, match="no PAT available"):
                await setup_worktree(self.project, self.dir_name, self.branch)
