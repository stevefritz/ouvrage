"""Tests for git provider interface, credential management, and schema changes."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Schema: git_credentials table
# ---------------------------------------------------------------------------

class TestGitCredentialsSchema:
    """Verify git_credentials table creation and CRUD."""

    async def test_create_credential(self, db):
        cred = await db.create_credential(
            provider="github", credential="encrypted_token_123", hostname="github.com",
        )
        assert cred["id"] is not None
        assert cred["provider"] == "github"
        assert cred["credential"] == "encrypted_token_123"
        assert cred["hostname"] == "github.com"
        assert cred["created_at"] is not None

    async def test_get_credential_by_provider(self, db):
        await db.create_credential(
            provider="github", credential="tok1", hostname="github.com",
        )
        result = await db.get_credential_by_provider("github")
        assert result is not None
        assert result["provider"] == "github"

    async def test_get_credential_by_provider_returns_none(self, db):
        result = await db.get_credential_by_provider("gitlab")
        assert result is None

    async def test_get_credential_by_hostname(self, db):
        await db.create_credential(
            provider="gitlab", credential="tok2", hostname="gl.sf.net",
        )
        result = await db.get_credential_by_hostname("gl.sf.net")
        assert result is not None
        assert result["provider"] == "gitlab"

    async def test_get_credential_by_hostname_returns_none(self, db):
        result = await db.get_credential_by_hostname("unknown.example.com")
        assert result is None

    async def test_list_credentials(self, db):
        await db.create_credential(provider="github", credential="tok1", hostname="github.com")
        await db.create_credential(provider="gitlab", credential="tok2", hostname="gitlab.com")
        creds = await db.list_credentials()
        assert len(creds) == 2

    async def test_update_credential(self, db):
        cred = await db.create_credential(
            provider="github", credential="old_tok", hostname="github.com",
        )
        updated = await db.update_credential(cred["id"], credential="new_tok")
        assert updated["credential"] == "new_tok"

    async def test_delete_credential(self, db):
        cred = await db.create_credential(
            provider="github", credential="tok1", hostname="github.com",
        )
        deleted = await db.delete_credential(cred["id"])
        assert deleted is True
        result = await db.get_credential_by_provider("github")
        assert result is None

    async def test_default_hostname_for_github(self, db):
        cred = await db.create_credential(provider="github", credential="tok1")
        assert cred["hostname"] == "github.com"

    async def test_default_hostname_for_gitlab(self, db):
        cred = await db.create_credential(provider="gitlab", credential="tok1")
        assert cred["hostname"] == "gitlab.com"

    async def test_default_hostname_for_bitbucket(self, db):
        cred = await db.create_credential(provider="bitbucket", credential="tok1")
        assert cred["hostname"] == "bitbucket.org"


# ---------------------------------------------------------------------------
# Schema: projects table — provider and credential_override columns
# ---------------------------------------------------------------------------

class TestProjectProviderColumns:
    """Verify new provider and credential_override columns on projects."""

    async def test_create_project_with_provider(self, db):
        project = await db.create_project(
            id="test-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/test", provider="github",
        )
        assert project["provider"] == "github"

    async def test_create_project_with_credential_override(self, db):
        project = await db.create_project(
            id="test-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/test", credential_override="encrypted_cred",
        )
        assert project["credential_override"] == "encrypted_cred"
        # Also stored in github_pat_override for backward compat
        assert project["github_pat_override"] == "encrypted_cred"

    async def test_create_project_github_pat_override_alias(self, db):
        """github_pat_override still works as alias for credential_override."""
        project = await db.create_project(
            id="test-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/test", github_pat_override="old_style_pat",
        )
        assert project["credential_override"] == "old_style_pat"
        assert project["github_pat_override"] == "old_style_pat"

    async def test_create_project_without_provider(self, db):
        project = await db.create_project(
            id="test-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/test",
        )
        assert project["provider"] is None

    async def test_update_project_credential_override(self, db):
        await db.create_project(
            id="test-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/test",
        )
        updated = await db.update_project("test-proj", credential_override="new_cred")
        assert updated["credential_override"] == "new_cred"
        # Also synced to github_pat_override
        assert updated["github_pat_override"] == "new_cred"

    async def test_update_project_provider(self, db):
        await db.create_project(
            id="test-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/test",
        )
        updated = await db.update_project("test-proj", provider="gitlab")
        assert updated["provider"] == "gitlab"


# ---------------------------------------------------------------------------
# detect_provider
# ---------------------------------------------------------------------------

class TestDetectProvider:
    """Test provider detection from URLs."""

    async def test_github_https(self, db):
        from switchboard.git.providers import detect_provider
        result = await detect_provider("https://github.com/acme/repo.git")
        assert result == "github"

    async def test_github_ssh(self, db):
        from switchboard.git.providers import detect_provider
        result = await detect_provider("git@github.com:acme/repo.git")
        assert result == "github"

    async def test_gitlab_https(self, db):
        from switchboard.git.providers import detect_provider
        result = await detect_provider("https://gitlab.com/acme/repo.git")
        assert result == "gitlab"

    async def test_bitbucket_https(self, db):
        from switchboard.git.providers import detect_provider
        result = await detect_provider("https://bitbucket.org/acme/repo.git")
        assert result == "bitbucket"

    async def test_unknown_host_returns_none(self, db):
        from switchboard.git.providers import detect_provider
        result = await detect_provider("https://unknown.example.com/acme/repo.git")
        assert result is None

    async def test_custom_hostname_from_credentials(self, db):
        """Custom hostnames registered in git_credentials are detected."""
        from switchboard.git.providers import detect_provider
        await db.create_credential(
            provider="gitlab", credential="tok", hostname="gl.sf.net",
        )
        result = await detect_provider("https://gl.sf.net/acme/repo.git")
        assert result == "gitlab"

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
    def test_https_url(self):
        from switchboard.git.providers import _parse_hostname
        assert _parse_hostname("https://github.com/owner/repo.git") == "github.com"

    def test_ssh_url(self):
        from switchboard.git.providers import _parse_hostname
        assert _parse_hostname("git@github.com:owner/repo.git") == "github.com"

    def test_custom_hostname(self):
        from switchboard.git.providers import _parse_hostname
        assert _parse_hostname("https://gl.sf.net/group/project.git") == "gl.sf.net"

    def test_ssh_custom_hostname(self):
        from switchboard.git.providers import _parse_hostname
        assert _parse_hostname("git@gl.sf.net:group/project.git") == "gl.sf.net"

    def test_invalid_url_returns_none(self):
        from switchboard.git.providers import _parse_hostname
        assert _parse_hostname("not-a-url") is None


# ---------------------------------------------------------------------------
# resolve_credential
# ---------------------------------------------------------------------------

class TestResolveCredential:
    """Test credential resolution chain."""

    @pytest.fixture(autouse=True)
    def _use_real_resolve_credential(self, real_resolve_credential):
        # This class tests resolve_credential itself — opt out of the
        # autouse mock so we exercise the real function.
        pass

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

    async def test_project_github_pat_override_alias(self, db):
        """Legacy github_pat_override field is used as fallback."""
        from switchboard.git.providers import resolve_credential
        from switchboard.crypto import encrypt_value

        encrypted = encrypt_value("legacy-pat")
        project = await db.create_project(
            id="test-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/test", github_pat_override=encrypted,
            provider="github",
        )
        provider, credential = await resolve_credential(project)
        assert credential == "legacy-pat"

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

    async def test_legacy_instance_github_pat(self, db):
        """Falls back to instance.github_pat_encrypted for github provider."""
        from switchboard.git.providers import resolve_credential

        await db.set_instance_github_pat("legacy-instance-pat")
        project = await db.create_project(
            id="test-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/test", provider="github",
        )
        provider, credential = await resolve_credential(project)
        assert credential == "legacy-instance-pat"

    async def test_no_credential_raises_error(self, db):
        """ValueError raised when no credential is available."""
        from switchboard.git.providers import resolve_credential

        project = await db.create_project(
            id="test-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/test", provider="github",
        )
        with pytest.raises(ValueError, match="No github credential configured"):
            await resolve_credential(project)

    async def test_defaults_to_github_provider(self, db):
        """Provider defaults to github when project.provider is None."""
        from switchboard.git.providers import resolve_credential

        await db.set_instance_github_pat("some-pat")
        project = await db.create_project(
            id="test-proj", repo="https://github.com/acme/test.git",
            working_dir="/tmp/test",
        )
        provider, credential = await resolve_credential(project)
        assert provider.name == "github"


# ---------------------------------------------------------------------------
# GitHubProvider
# ---------------------------------------------------------------------------

class TestGitHubProvider:
    def setup_method(self):
        from switchboard.git.providers.github import GitHubProvider
        self.provider = GitHubProvider()

    def test_name(self):
        assert self.provider.name == "github"

    def test_default_hostname(self):
        assert self.provider.default_hostname == "github.com"

    def test_parse_repo_url_https(self):
        info = self.provider.parse_repo_url("https://github.com/acme/widgets.git")
        assert info.owner == "acme"
        assert info.repo == "widgets"
        assert info.hostname == "github.com"

    def test_parse_repo_url_ssh(self):
        info = self.provider.parse_repo_url("git@github.com:acme/widgets.git")
        assert info.owner == "acme"
        assert info.repo == "widgets"

    def test_parse_repo_url_no_git_suffix(self):
        info = self.provider.parse_repo_url("https://github.com/acme/widgets")
        assert info.owner == "acme"
        assert info.repo == "widgets"

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
    def test_get_github(self):
        from switchboard.git.providers import get_provider
        p = get_provider("github")
        assert p.name == "github"

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

    async def test_migration_does_not_duplicate(self, db):
        """Running init_db twice doesn't create duplicate credentials."""
        from switchboard.crypto import encrypt_value

        encrypted = encrypt_value("test-pat-123")
        await db.update_instance(github_pat_encrypted=encrypted)

        await db.init_db()
        await db.init_db()

        creds = await db.list_credentials()
        github_creds = [c for c in creds if c["provider"] == "github"]
        assert len(github_creds) == 1


# ---------------------------------------------------------------------------
# Dispatch-time credential validation
# ---------------------------------------------------------------------------

class TestDispatchCredentialValidation:
    """Test that dispatch blocks when no credential is available."""

    @pytest.fixture(autouse=True)
    def _use_real_resolve_credential(self, real_resolve_credential):
        # This class verifies dispatch's credential pre-flight — opt out of
        # the autouse mock so the real resolution path runs.
        pass

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
