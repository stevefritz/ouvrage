"""Tests confirming the internal #/docs route has been completely purged.

The #/docs route was a dev-only internal architecture reference page that was
never meant to be customer-facing. These tests verify:
- The dashboard/docs/ directory and architecture.js no longer exist
- No dashboard JS file references the docs route or ArchitectureDocs component
- The router helper for #/docs has been removed
"""

import os
import re
import pytest

DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "..", "dashboard")


class TestDocsPagePurged:
    def test_docs_directory_does_not_exist(self):
        docs_dir = os.path.join(DASHBOARD_DIR, "docs")
        assert not os.path.exists(docs_dir), (
            "dashboard/docs/ directory still exists — it should have been deleted"
        )

    def test_architecture_js_does_not_exist(self):
        arch_file = os.path.join(DASHBOARD_DIR, "docs", "architecture.js")
        assert not os.path.exists(arch_file), (
            "dashboard/docs/architecture.js still exists — it should have been deleted"
        )

    def test_ouvrage_app_has_no_architecture_import(self):
        app_path = os.path.join(DASHBOARD_DIR, "ouvrage-app.js")
        content = open(app_path).read()
        assert "architecture" not in content, (
            "ouvrage-app.js still imports from docs/architecture.js"
        )
        assert "ArchitectureDocs" not in content, (
            "ouvrage-app.js still references ArchitectureDocs"
        )

    def test_ouvrage_app_has_no_docs_route(self):
        app_path = os.path.join(DASHBOARD_DIR, "ouvrage-app.js")
        content = open(app_path).read()
        assert "view === 'docs'" not in content, (
            "ouvrage-app.js still has a 'docs' view branch"
        )

    def test_ouvrage_shell_has_no_docs_nav_link(self):
        shell_path = os.path.join(DASHBOARD_DIR, "ouvrage-shell.js")
        content = open(shell_path).read()
        assert "#/docs" not in content, (
            "ouvrage-shell.js still has a #/docs nav link"
        )

    def test_router_has_no_docs_helper(self):
        router_path = os.path.join(DASHBOARD_DIR, "router.js")
        content = open(router_path).read()
        # Check the routes export object doesn't have a docs key
        assert re.search(r'\bdocs\s*:', content) is None, (
            "router.js still has a docs: route helper"
        )

    def test_no_dashboard_file_references_docs_route(self):
        """Sweep all dashboard JS files for any remaining #/docs references."""
        for root, dirs, files in os.walk(DASHBOARD_DIR):
            for fname in files:
                if not fname.endswith(".js"):
                    continue
                fpath = os.path.join(root, fname)
                content = open(fpath).read()
                assert "#/docs" not in content, (
                    f"{fpath} still contains a #/docs reference"
                )
                assert "ArchitectureDocs" not in content, (
                    f"{fpath} still references ArchitectureDocs"
                )
