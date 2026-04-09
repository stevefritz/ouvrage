"""Tests for git provider interface, credential management, and schema changes."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Schema: git_credentials table
# ---------------------------------------------------------------------------

class TestGitCredentialsSchema:
    """Verify git_credentials table creation and CRUD."""


    async def test_default_hostname_for_bitbucket(self, db):
        cred = await db.create_credential(provider="bitbucket", credential="tok1")
        assert cred["hostname"] == "bitbucket.org"


# ---------------------------------------------------------------------------
# Schema: projects table — provider and credential_override columns
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# detect_provider
# ---------------------------------------------------------------------------

class TestDetectProvider:
    """Test provider detection from URLs."""


    async def test_github_ssh(self, db):
        from switchboard.git.providers import detect_provider
        result = await detect_provider("git@github.com:acme/repo.git")
        assert result == "github"


    async def test_credential_hostname_takes_priority(self, db):
        """DB hostname check runs before hardcoded defaults."""
        from switchboard.git.providers import detect_provider
        # Register github.com as a different provider (contrived but tests priority)
        await db.create_credential(
            provider="gitlab", credential="tok", hostname="github.com",
        )
        result = await detect_provider("https://github.com/acme/repo.git")
        assert result == "gitlab"


# ---------------------------------------------------------------------------
# _parse_hostname
# ---------------------------------------------------------------------------

class TestParseHostname:


    def test_invalid_url_returns_none(self):
        from switchboard.git.providers import _parse_hostname
        assert _parse_hostname("not-a-url") is None


# ---------------------------------------------------------------------------
# resolve_credential
# ---------------------------------------------------------------------------

class TestResolveCredential:
    """Test credential resolution chain."""

    async def test_project_credential_override(self, db):
        """Project-level credential_override is used first."""
        from switchboard.git.providers import resolve_credential
        from switchboard.crypto import encrypt_value

        encrypted = encrypt_value("my-secret-pat")
        project = await db.create_project(
            id="test-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/test", credential_override=encrypted,
            provider="github",
        )
        provider, credential = await resolve_credential(project)
        assert provider.name == "github"
        assert credential == "my-secret-pat"


    async def test_instance_credential_from_git_credentials(self, db):
        """Instance-level credential from git_credentials table."""
        from switchboard.git.providers import resolve_credential
        from switchboard.crypto import encrypt_value

        encrypted = encrypt_value("instance-token")
        await db.create_credential(
            provider="github", credential=encrypted, hostname="github.com",
        )
        project = await db.create_project(
            id="test-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/test", provider="github",
        )
        provider, credential = await resolve_credential(project)
        assert credential == "instance-token"


# ---------------------------------------------------------------------------
# GitHubProvider
# ---------------------------------------------------------------------------

class TestGitHubProvider:
    def setup_method(self):
        from switchboard.git.providers.github import GitHubProvider
        self.provider = GitHubProvider()


    def test_default_hostname(self):
        assert self.provider.default_hostname == "github.com"


    def test_parse_repo_url_invalid(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            self.provider.parse_repo_url("https://gitlab.com/acme/repo.git")

    def test_build_authenticated_url(self):
        url = self.provider.build_authenticated_url(
            "https://github.com/acme/widgets.git", "my-token",
        )
        assert url == "https://oauth2:my-token@github.com/acme/widgets.git"

    def test_build_authenticated_url_ssh_input(self):
        url = self.provider.build_authenticated_url(
            "git@github.com:acme/widgets.git", "my-token",
        )
        assert url == "https://oauth2:my-token@github.com/acme/widgets.git"


# ---------------------------------------------------------------------------
# get_provider
# ---------------------------------------------------------------------------

class TestGetProvider:

    def test_unknown_provider_raises(self):
        from switchboard.git.providers import get_provider
        with pytest.raises(ValueError, match="Unknown git provider"):
            get_provider("svn")


# ---------------------------------------------------------------------------
# Schema auto-migration: instance PAT → git_credentials
# ---------------------------------------------------------------------------

class TestSchemaAutoMigration:
    """Test that startup migration moves instance PAT to git_credentials."""

    async def test_instance_pat_migrated_to_git_credentials(self, db):
        """After init_db, instance.github_pat_encrypted should create a git_credentials row."""
        from switchboard.crypto import encrypt_value

        # Set an instance PAT
        encrypted = encrypt_value("test-pat-123")
        await db.update_instance(github_pat_encrypted=encrypted)

        # Re-run init_db to trigger migration
        await db.init_db()

        # Should have a git_credentials row for github
        cred = await db.get_credential_by_provider("github")
        assert cred is not None
        assert cred["hostname"] == "github.com"
        assert cred["credential"] == encrypted


# ---------------------------------------------------------------------------
# Dispatch-time credential validation
# ---------------------------------------------------------------------------

class TestDispatchCredentialValidation:
    """Test that dispatch blocks when no credential is available."""

    @pytest.fixture(autouse=True)
    def _mock_launch_patches(self):
        """Mock git/SDK operations to isolate credential validation."""
        mocks = {
            "run_as_worker": AsyncMock(return_value=(b"", b"", 0)),
            "setup_worktree": AsyncMock(return_value="/tmp/fake-worktree"),
            "cleanup_worktree": AsyncMock(),
            "ensure_branch_pushed": AsyncMock(return_value=True),
            "setup_hook_config": AsyncMock(),
        }
        patches = [
            patch("switchboard.dispatch.engine._run_as_worker", mocks["run_as_worker"]),
            patch("switchboard.dispatch.engine.setup_worktree", mocks["setup_worktree"]),
            patch("switchboard.dispatch.engine.cleanup_worktree", mocks["cleanup_worktree"]),
            patch("switchboard.git.operations._ensure_branch_pushed", mocks["ensure_branch_pushed"]),
            patch("switchboard.dispatch.internals.setup_hook_config", mocks["setup_hook_config"]),
        ]
        for p in patches:
            p.start()
        yield mocks
        for p in patches:
            p.stop()

    async def test_dispatch_blocks_without_credential(self, db):
        """Dispatch raises ValueError when no credential is available."""
        from switchboard.dispatch.engine import dispatch_task
        import switchboard.config.settings as _settings

        await db.create_project(
            id="no-cred-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/no-cred", provider="github",
        )

        orig = _settings.SKIP_CREDENTIAL_CHECK
        _settings.SKIP_CREDENTIAL_CHECK = False
        try:
            with pytest.raises(ValueError, match="No github credential configured"):
                await dispatch_task(
                    project_id="no-cred-proj",
                    task_id="no-cred-proj/test-task",
                    goal="Test task",
                )
        finally:
            _settings.SKIP_CREDENTIAL_CHECK = orig

    async def test_dispatch_succeeds_with_credential(self, db):
        """Dispatch proceeds past credential check when credential is available."""
        from switchboard.git.providers import resolve_credential

        await db.set_instance_github_pat("valid-pat")
        project = await db.create_project(
            id="cred-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/cred", provider="github",
        )

        # Verify resolve_credential succeeds (this is what dispatch checks)
        provider, credential = await resolve_credential(project)
        assert provider.name == "github"
        assert credential == "valid-pat"
