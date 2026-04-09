"""Tests for HTTPS URL normalization and authenticated bare clone."""

import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from switchboard.git.operations import normalize_repo_url


# ---------------------------------------------------------------------------
# normalize_repo_url
# ---------------------------------------------------------------------------

class TestNormalizeRepoUrl:
    """normalize_repo_url must return canonical HTTPS format for any provider."""


    def test_http_scheme_upgraded(self):
        assert normalize_repo_url("http://github.com/acme/widgets") == "https://github.com/acme/widgets.git"


    def test_unknown_format_passthrough(self):
        result = normalize_repo_url("not-a-url")
        assert result == "not-a-url"


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
            patch("switchboard.server.handlers.projects._run_project_validation", AsyncMock(side_effect=lambda pid, proj: proj)),
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

        async def mock_validation(project_id, project):
            return project

        self.patches = [
            patch("switchboard.server.handlers.projects.db.update_project", side_effect=mock_update_project),
            patch("switchboard.server.handlers.projects._run_project_validation", side_effect=mock_validation),
        ]
        for p in self.patches:
            p.start()
        yield
        for p in self.patches:
            p.stop()


    @pytest.mark.asyncio
    async def test_https_repo_unchanged_on_update(self):
        from switchboard.server.handlers.projects import _handle_update_project
        await _handle_update_project({
            "id": "test-proj",
            "repo": "https://github.com/acme/widgets",
        })
        assert self.updated_fields["repo"] == "https://github.com/acme/widgets.git"


# ---------------------------------------------------------------------------
# Startup migration — SSH URLs in DB converted to HTTPS
# ---------------------------------------------------------------------------

class TestStartupMigration:
    """init_db migration must convert SSH repo URLs to HTTPS."""


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
