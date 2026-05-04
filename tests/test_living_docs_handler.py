"""Integration tests for Living Docs MCP tool handlers via _dispatch_tool."""

from unittest.mock import AsyncMock, patch

import pytest

from ouvrage.server.dispatch import _dispatch_tool


# ---------------------------------------------------------------------------
# Shared sample data
# ---------------------------------------------------------------------------

_CONFIG_ROW = {
    "id": 1,
    "project_id": "my-project",
    "slug": "architecture",
    "title": "Architecture Overview",
    "brief": "High-level system architecture.",
    "source_hints": None,
    "last_seen_sha": None,
    "last_regen_at": None,
    "created_at": "2026-01-01T00:00:00Z",
}


# ---------------------------------------------------------------------------
# set_reference_doc_config
# ---------------------------------------------------------------------------

class TestSetReferenceDocConfig:
    @pytest.fixture(autouse=True)
    def mock_set_config(self):
        with patch(
            "ouvrage.server.handlers.living_docs_handler.set_config",
            new_callable=AsyncMock,
            return_value=_CONFIG_ROW,
        ) as m:
            yield m

    async def test_happy_path(self, mock_set_config):
        result = await _dispatch_tool("set_reference_doc_config", {
            "project_id": "my-project",
            "slug": "architecture",
            "title": "Architecture Overview",
            "brief": "High-level system architecture.",
        })
        assert result["slug"] == "architecture"
        assert result["project_id"] == "my-project"
        mock_set_config.assert_awaited_once_with(
            project_id="my-project",
            slug="architecture",
            title="Architecture Overview",
            brief="High-level system architecture.",
            source_hints=None,
        )

    async def test_with_source_hints(self, mock_set_config):
        await _dispatch_tool("set_reference_doc_config", {
            "project_id": "my-project",
            "slug": "architecture",
            "title": "Architecture Overview",
            "brief": "Brief.",
            "source_hints": "See ouvrage/server/",
        })
        mock_set_config.assert_awaited_once_with(
            project_id="my-project",
            slug="architecture",
            title="Architecture Overview",
            brief="Brief.",
            source_hints="See ouvrage/server/",
        )

    async def test_missing_project_id_raises(self):
        with pytest.raises(ValueError, match="project_id is required"):
            await _dispatch_tool("set_reference_doc_config", {
                "slug": "architecture",
                "title": "Title",
                "brief": "Brief",
            })

    async def test_missing_slug_raises(self):
        with pytest.raises(ValueError, match="slug is required"):
            await _dispatch_tool("set_reference_doc_config", {
                "project_id": "my-project",
                "title": "Title",
                "brief": "Brief",
            })

    async def test_missing_brief_raises(self):
        with pytest.raises(ValueError, match="brief is required"):
            await _dispatch_tool("set_reference_doc_config", {
                "project_id": "my-project",
                "slug": "architecture",
                "title": "Title",
            })


# ---------------------------------------------------------------------------
# delete_reference_doc_config
# ---------------------------------------------------------------------------

class TestDeleteReferenceDocConfig:
    @pytest.fixture(autouse=True)
    def mock_delete_config(self):
        with patch(
            "ouvrage.server.handlers.living_docs_handler.delete_config",
            new_callable=AsyncMock,
            return_value=None,
        ) as m:
            yield m

    async def test_returns_deleted_true(self, mock_delete_config):
        result = await _dispatch_tool("delete_reference_doc_config", {
            "project_id": "my-project",
            "slug": "architecture",
        })
        assert result == {"deleted": True}
        mock_delete_config.assert_awaited_once_with("my-project", "architecture")

    async def test_missing_project_id_raises(self):
        with pytest.raises(ValueError, match="project_id is required"):
            await _dispatch_tool("delete_reference_doc_config", {"slug": "architecture"})

    async def test_missing_slug_raises(self):
        with pytest.raises(ValueError, match="slug is required"):
            await _dispatch_tool("delete_reference_doc_config", {"project_id": "my-project"})


# ---------------------------------------------------------------------------
# set_living_docs_enabled
# ---------------------------------------------------------------------------

class TestSetLivingDocsEnabled:
    @pytest.fixture(autouse=True)
    def mock_db(self):
        with (
            patch(
                "ouvrage.server.handlers.living_docs_handler.db.get_project",
                new_callable=AsyncMock,
                return_value={"id": "my-project"},
            ),
            patch(
                "ouvrage.server.handlers.living_docs_handler.db.update_project",
                new_callable=AsyncMock,
                return_value={"id": "my-project", "living_docs_enabled": True},
            ) as mock_update,
        ):
            self._mock_update = mock_update
            yield mock_update

    async def test_enable(self):
        result = await _dispatch_tool("set_living_docs_enabled", {
            "project_id": "my-project",
            "enabled": True,
        })
        assert result == {"enabled": True}
        self._mock_update.assert_awaited_once_with("my-project", living_docs_enabled=True)

    async def test_disable(self):
        result = await _dispatch_tool("set_living_docs_enabled", {
            "project_id": "my-project",
            "enabled": False,
        })
        assert result == {"enabled": False}

    async def test_project_not_found_raises(self):
        with patch(
            "ouvrage.server.handlers.living_docs_handler.db.get_project",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(ValueError, match="not found"):
                await _dispatch_tool("set_living_docs_enabled", {
                    "project_id": "nonexistent",
                    "enabled": True,
                })

    async def test_missing_project_id_raises(self):
        with pytest.raises(ValueError, match="project_id is required"):
            await _dispatch_tool("set_living_docs_enabled", {"enabled": True})

    async def test_missing_enabled_raises(self):
        with pytest.raises(ValueError, match="enabled is required"):
            await _dispatch_tool("set_living_docs_enabled", {"project_id": "my-project"})


# ---------------------------------------------------------------------------
# add_reference_doc_version
# ---------------------------------------------------------------------------

class TestAddReferenceDocVersion:
    _SERVICE_RESULT = {
        "file_id": "abc-123",
        "stored_path": "/data/reference_docs/my-project/architecture.md",
        "embedded": "queued",
        "chunkable": True,
    }

    async def test_worker_check_rejects_non_worker(self):
        with patch(
            "ouvrage.server.handlers.living_docs_handler.get_request_is_worker",
            return_value=False,
        ):
            with pytest.raises(ValueError, match="only available on the worker endpoint"):
                await _dispatch_tool("add_reference_doc_version", {
                    "task_id": "my-project/task-1",
                    "slug": "architecture",
                    "source_path": "/work/my-project/task-1/docs/architecture.md",
                })

    async def test_worker_succeeds(self):
        with (
            patch(
                "ouvrage.server.handlers.living_docs_handler.get_request_is_worker",
                return_value=True,
            ),
            patch(
                "ouvrage.server.handlers.living_docs_handler.add_version",
                new_callable=AsyncMock,
                return_value=self._SERVICE_RESULT,
            ) as mock_add,
        ):
            result = await _dispatch_tool("add_reference_doc_version", {
                "task_id": "my-project/task-1",
                "slug": "architecture",
                "source_path": "/work/my-project/task-1/docs/architecture.md",
            })
            assert result["file_id"] == "abc-123"
            assert result["embedded"] == "queued"
            mock_add.assert_awaited_once_with(
                task_id="my-project/task-1",
                slug="architecture",
                source_path="/work/my-project/task-1/docs/architecture.md",
            )

    async def test_missing_task_id_raises_for_worker(self):
        with (
            patch(
                "ouvrage.server.handlers.living_docs_handler.get_request_is_worker",
                return_value=True,
            ),
            patch(
                "ouvrage.server.handlers.living_docs_handler.add_version",
                new_callable=AsyncMock,
            ),
        ):
            with pytest.raises(ValueError, match="task_id is required"):
                await _dispatch_tool("add_reference_doc_version", {
                    "slug": "architecture",
                    "source_path": "/work/task-1/docs/architecture.md",
                })


# ---------------------------------------------------------------------------
# list_reference_doc_configs
# ---------------------------------------------------------------------------

class TestListReferenceDocConfigs:
    @pytest.fixture(autouse=True)
    def mock_list_configs(self):
        with patch(
            "ouvrage.server.handlers.living_docs_handler.list_configs",
            new_callable=AsyncMock,
            return_value=[_CONFIG_ROW],
        ) as m:
            yield m

    async def test_returns_configs_list(self, mock_list_configs):
        result = await _dispatch_tool("list_reference_doc_configs", {
            "project_id": "my-project",
        })
        assert "configs" in result
        assert len(result["configs"]) == 1
        assert result["configs"][0]["slug"] == "architecture"
        mock_list_configs.assert_awaited_once_with("my-project")

    async def test_empty_project(self):
        with patch(
            "ouvrage.server.handlers.living_docs_handler.list_configs",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await _dispatch_tool("list_reference_doc_configs", {
                "project_id": "empty-project",
            })
            assert result == {"configs": []}

    async def test_missing_project_id_raises(self):
        with pytest.raises(ValueError, match="project_id is required"):
            await _dispatch_tool("list_reference_doc_configs", {})


# ---------------------------------------------------------------------------
# get_reference_doc_config
# ---------------------------------------------------------------------------

class TestGetReferenceDocConfig:
    async def test_returns_config_with_local_copy_present(self):
        with (
            patch(
                "ouvrage.server.handlers.living_docs_handler.get_config",
                new_callable=AsyncMock,
                return_value=_CONFIG_ROW,
            ),
            patch(
                "ouvrage.server.handlers.living_docs_handler.get_local_copy",
                new_callable=AsyncMock,
                return_value="# Architecture\n\nContent here.",
            ),
        ):
            result = await _dispatch_tool("get_reference_doc_config", {
                "project_id": "my-project",
                "slug": "architecture",
            })
            assert result["slug"] == "architecture"
            assert result["local_copy_present"] is True

    async def test_returns_config_with_no_local_copy(self):
        with (
            patch(
                "ouvrage.server.handlers.living_docs_handler.get_config",
                new_callable=AsyncMock,
                return_value=_CONFIG_ROW,
            ),
            patch(
                "ouvrage.server.handlers.living_docs_handler.get_local_copy",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await _dispatch_tool("get_reference_doc_config", {
                "project_id": "my-project",
                "slug": "architecture",
            })
            assert result["local_copy_present"] is False

    async def test_not_found_raises(self):
        with patch(
            "ouvrage.server.handlers.living_docs_handler.get_config",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(ValueError, match="not found"):
                await _dispatch_tool("get_reference_doc_config", {
                    "project_id": "my-project",
                    "slug": "nonexistent",
                })

    async def test_missing_project_id_raises(self):
        with pytest.raises(ValueError, match="project_id is required"):
            await _dispatch_tool("get_reference_doc_config", {"slug": "architecture"})

    async def test_missing_slug_raises(self):
        with pytest.raises(ValueError, match="slug is required"):
            await _dispatch_tool("get_reference_doc_config", {"project_id": "my-project"})
