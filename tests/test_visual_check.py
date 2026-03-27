"""Tests for the visual-check screenshot tool.

Tests config loading, fixture reading, and CLI validation — not actual
screenshots (those require Chromium).
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add scripts dir to path so we can import
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Import functions from the script module
import importlib.util

spec = importlib.util.spec_from_file_location(
    "visual_check", SCRIPTS_DIR / "visual-check.py"
)
visual_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(visual_check)


class TestLoadConfig:
    """Tests for load_config()."""

    def test_loads_valid_config(self):
        config = visual_check.load_config(SCRIPTS_DIR / "visual-config.json")
        assert "pages" in config
        assert "settings" in config["pages"]
        assert "settings-mobile" in config["pages"]
        assert "landing" in config["pages"]

    def test_config_has_required_fields(self):
        config = visual_check.load_config(SCRIPTS_DIR / "visual-config.json")
        for name, page in config["pages"].items():
            assert "url" in page, f"Page {name} missing 'url'"
            assert "viewport" in page, f"Page {name} missing 'viewport'"
            assert "width" in page["viewport"], f"Page {name} missing viewport width"
            assert "height" in page["viewport"], f"Page {name} missing viewport height"

    def test_missing_config_raises(self):
        with pytest.raises(FileNotFoundError):
            visual_check.load_config("/nonexistent/path.json")

    def test_settings_page_has_correct_viewport(self):
        config = visual_check.load_config(SCRIPTS_DIR / "visual-config.json")
        settings = config["pages"]["settings"]
        assert settings["viewport"]["width"] == 1280
        assert settings["viewport"]["height"] == 900

    def test_mobile_page_has_mobile_viewport(self):
        config = visual_check.load_config(SCRIPTS_DIR / "visual-config.json")
        mobile = config["pages"]["settings-mobile"]
        assert mobile["viewport"]["width"] == 375
        assert mobile["viewport"]["height"] == 812


class TestLoadFixture:
    """Tests for load_fixture()."""

    def test_loads_settings_instance(self):
        base_dir = SCRIPTS_DIR.parent
        content = visual_check.load_fixture(
            "fixtures/visual/settings-instance.json", base_dir
        )
        data = json.loads(content)
        assert data["instance"]["name"] == "Acme Corp"
        assert data["github"]["connected"] is True

    def test_loads_settings_user(self):
        base_dir = SCRIPTS_DIR.parent
        content = visual_check.load_fixture(
            "fixtures/visual/settings-user.json", base_dir
        )
        data = json.loads(content)
        assert data["profile"]["name"] == "Stephen Fritz"
        assert data["anthropic"]["configured"] is True

    def test_loads_projects(self):
        base_dir = SCRIPTS_DIR.parent
        content = visual_check.load_fixture(
            "fixtures/visual/projects.json", base_dir
        )
        data = json.loads(content)
        assert isinstance(data, list)
        assert len(data) >= 4

    def test_missing_fixture_raises(self):
        with pytest.raises(FileNotFoundError):
            visual_check.load_fixture("nonexistent.json", SCRIPTS_DIR.parent)


class TestFindFreePort:
    """Tests for find_free_port()."""

    def test_returns_valid_port(self):
        port = visual_check.find_free_port()
        assert isinstance(port, int)
        assert 1024 <= port <= 65535

    def test_returns_different_ports(self):
        # Not guaranteed but extremely likely
        ports = {visual_check.find_free_port() for _ in range(5)}
        assert len(ports) >= 2


class TestConfigMockPaths:
    """Tests that all mock fixture paths in config actually exist."""

    def test_all_mock_fixtures_exist(self):
        config = visual_check.load_config(SCRIPTS_DIR / "visual-config.json")
        base_dir = SCRIPTS_DIR.parent
        for page_name, page_config in config["pages"].items():
            for api_path, fixture_path in page_config.get("mocks", {}).items():
                full_path = base_dir / fixture_path
                assert full_path.exists(), (
                    f"Mock fixture missing for {page_name} {api_path}: {full_path}"
                )

    def test_reference_image_exists_for_settings(self):
        config = visual_check.load_config(SCRIPTS_DIR / "visual-config.json")
        base_dir = SCRIPTS_DIR.parent
        ref = config["pages"]["settings"].get("reference")
        assert ref is not None
        assert (base_dir / ref).exists(), f"Reference image missing: {ref}"
