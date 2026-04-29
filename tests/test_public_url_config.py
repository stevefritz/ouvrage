"""Tests for OUVRAGE_PUBLIC_URL precedence in ouvrage.config.settings.

OUVRAGE_PUBLIC_URL provides defaults for OAUTH_BASE_URL, AUTH_ISSUER_URL,
and RESOURCE_URL. The three explicit env vars must still take precedence
when set, for split-domain / SaaS deployments.
"""
import importlib
import sys
import pytest


def _reload_settings(monkeypatch, env: dict):
    """Reload ouvrage.config.settings under the given env, return module."""
    # Clear all the env vars we care about, then apply the requested set
    for k in (
        "OUVRAGE_PUBLIC_URL",
        "OAUTH_BASE_URL",
        "AUTH_ISSUER_URL",
        "RESOURCE_URL",
    ):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    # Force a fresh import so module-level os.environ.get() lookups re-run
    sys.modules.pop("ouvrage.config.settings", None)
    return importlib.import_module("ouvrage.config.settings")


class TestPublicUrlPrecedence:
    def test_no_env_vars_set_defaults_to_localhost_only(self, monkeypatch):
        s = _reload_settings(monkeypatch, {})
        assert s.PUBLIC_URL == ""
        assert s.OAUTH_BASE_URL is None
        assert s.AUTH_ISSUER_URL is None
        assert s.RESOURCE_URL is None

    def test_public_url_alone_propagates_to_all_three(self, monkeypatch):
        s = _reload_settings(
            monkeypatch,
            {"OUVRAGE_PUBLIC_URL": "https://you.ngrok.app"},
        )
        assert s.PUBLIC_URL == "https://you.ngrok.app"
        assert s.OAUTH_BASE_URL == "https://you.ngrok.app"
        assert s.AUTH_ISSUER_URL == "https://you.ngrok.app"
        assert s.RESOURCE_URL == "https://you.ngrok.app/mcp"

    def test_public_url_strips_trailing_slash(self, monkeypatch):
        s = _reload_settings(
            monkeypatch,
            {"OUVRAGE_PUBLIC_URL": "https://you.ngrok.app/"},
        )
        assert s.PUBLIC_URL == "https://you.ngrok.app"
        assert s.RESOURCE_URL == "https://you.ngrok.app/mcp"

    def test_explicit_oauth_base_url_overrides_public_url(self, monkeypatch):
        s = _reload_settings(
            monkeypatch,
            {
                "OUVRAGE_PUBLIC_URL": "https://you.ngrok.app",
                "OAUTH_BASE_URL": "https://oauth.example.com",
            },
        )
        assert s.OAUTH_BASE_URL == "https://oauth.example.com"
        # The other two still derive from PUBLIC_URL
        assert s.AUTH_ISSUER_URL == "https://you.ngrok.app"
        assert s.RESOURCE_URL == "https://you.ngrok.app/mcp"

    def test_explicit_auth_issuer_url_overrides_public_url(self, monkeypatch):
        s = _reload_settings(
            monkeypatch,
            {
                "OUVRAGE_PUBLIC_URL": "https://you.ngrok.app",
                "AUTH_ISSUER_URL": "https://issuer.example.com",
            },
        )
        assert s.AUTH_ISSUER_URL == "https://issuer.example.com"
        assert s.OAUTH_BASE_URL == "https://you.ngrok.app"
        assert s.RESOURCE_URL == "https://you.ngrok.app/mcp"

    def test_explicit_resource_url_overrides_public_url(self, monkeypatch):
        s = _reload_settings(
            monkeypatch,
            {
                "OUVRAGE_PUBLIC_URL": "https://you.ngrok.app",
                "RESOURCE_URL": "https://resource.example.com/mcp",
            },
        )
        assert s.RESOURCE_URL == "https://resource.example.com/mcp"
        assert s.OAUTH_BASE_URL == "https://you.ngrok.app"
        assert s.AUTH_ISSUER_URL == "https://you.ngrok.app"

    def test_legacy_three_var_setup_still_works_without_public_url(self, monkeypatch):
        """Backward compat: setups that set only the three explicit vars work as before."""
        s = _reload_settings(
            monkeypatch,
            {
                "OAUTH_BASE_URL": "https://a.example.com",
                "AUTH_ISSUER_URL": "https://b.example.com",
                "RESOURCE_URL": "https://c.example.com/mcp",
            },
        )
        assert s.PUBLIC_URL == ""
        assert s.OAUTH_BASE_URL == "https://a.example.com"
        assert s.AUTH_ISSUER_URL == "https://b.example.com"
        assert s.RESOURCE_URL == "https://c.example.com/mcp"


@pytest.fixture(autouse=True)
def _reset_settings_after_test():
    """Ensure settings module is fresh for any test that imports it later."""
    yield
    sys.modules.pop("ouvrage.config.settings", None)
