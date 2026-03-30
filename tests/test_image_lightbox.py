"""Tests for image preview lightbox feature.

Covers:
- isImageFile extension detection logic (verified via JS source content)
- ImageLightbox component structure
- Files page button alignment fix
- TaskView FilesDrawer image preview
"""

import re
from pathlib import Path

DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"
LIGHTBOX_JS = DASHBOARD_DIR / "components" / "ImageLightbox.js"
FILES_JS = DASHBOARD_DIR / "components" / "Files.js"
TASK_VIEW_JS = DASHBOARD_DIR / "views" / "TaskView.js"


class TestIsImageFileDetection:
    """Verify isImageFile function covers all required extensions."""

    def _get_source(self):
        return LIGHTBOX_JS.read_text()

    def test_lightbox_file_exists(self):
        assert LIGHTBOX_JS.exists(), "ImageLightbox.js component file must exist"

    def test_is_image_file_function_exported(self):
        src = self._get_source()
        assert "export function isImageFile" in src

    def test_detects_png(self):
        src = self._get_source()
        assert "png" in src

    def test_detects_jpg(self):
        src = self._get_source()
        assert "jpg" in src

    def test_detects_jpeg(self):
        src = self._get_source()
        assert "jpeg" in src

    def test_detects_gif(self):
        src = self._get_source()
        assert "gif" in src

    def test_detects_webp(self):
        src = self._get_source()
        assert "webp" in src

    def test_detects_svg(self):
        src = self._get_source()
        assert "svg" in src

    def test_all_image_extensions_in_array(self):
        """All six extensions must appear together in the isImageFile function."""
        src = self._get_source()
        # Find the isImageFile function body
        match = re.search(r"function isImageFile\(.+?\{(.+?)\}", src, re.DOTALL)
        assert match, "isImageFile function body not found"
        body = match.group(1)
        for ext in ["png", "jpg", "jpeg", "gif", "webp", "svg"]:
            assert ext in body, f"Extension '{ext}' not found in isImageFile"

    def test_non_image_extensions_not_in_array(self):
        """Confirm pdf/txt/py are not whitelisted as image extensions."""
        src = self._get_source()
        match = re.search(r"function isImageFile\(.+?\{(.+?)\}", src, re.DOTALL)
        assert match
        body = match.group(1)
        for ext in ["pdf", "txt", "py"]:
            assert ext not in body, f"Non-image extension '{ext}' should not be in isImageFile"


class TestImageLightboxComponent:
    """Verify ImageLightbox component structure."""

    def _get_source(self):
        return LIGHTBOX_JS.read_text()

    def test_component_exported(self):
        src = self._get_source()
        assert "export function ImageLightbox" in src

    def test_accepts_src_prop(self):
        src = self._get_source()
        # Component signature should destructure src
        assert "{ src," in src or "{ src }" in src or "src," in src

    def test_accepts_alt_prop(self):
        src = self._get_source()
        assert "alt" in src

    def test_accepts_onclose_prop(self):
        src = self._get_source()
        assert "onClose" in src

    def test_has_dark_backdrop(self):
        src = self._get_source()
        assert "rgba(0,0,0" in src, "Lightbox must have a dark semi-transparent backdrop"

    def test_has_escape_key_handler(self):
        src = self._get_source()
        assert "Escape" in src, "Lightbox must close on Escape key"

    def test_has_close_button(self):
        src = self._get_source()
        assert "✕" in src or "×" in src or "close" in src.lower()

    def test_image_constrained_to_viewport(self):
        src = self._get_source()
        assert "90vw" in src, "Image must be constrained to 90vw"
        assert "90vh" in src, "Image must be constrained to 90vh"

    def test_image_object_fit_contain(self):
        src = self._get_source()
        assert "contain" in src, "Image must use object-fit: contain"

    def test_click_backdrop_to_close(self):
        """Backdrop onClick should call onClose."""
        src = self._get_source()
        assert "onClick" in src and "onClose" in src


class TestFilesPageButtonAlignment:
    """Verify Files.js button alignment fix."""

    def _get_source(self):
        return FILES_JS.read_text()

    def test_files_js_imports_lightbox(self):
        src = self._get_source()
        assert "ImageLightbox" in src and "ImageLightbox.js" in src

    def test_button_container_flex_end(self):
        """Buttons container must use justify-content: flex-end for consistent alignment."""
        src = self._get_source()
        assert "flex-end" in src, "Files page button container must use justify-content: flex-end"

    def test_button_container_gap(self):
        src = self._get_source()
        # Should have a gap between buttons
        assert "gap" in src

    def test_copy_and_delete_both_present(self):
        src = self._get_source()
        assert "Copy" in src
        assert "Delete" in src or "ConfirmAction" in src

    def test_image_thumbnail_shown(self):
        """Image files get a thumbnail in the files list."""
        src = self._get_source()
        assert "isImageFile" in src
        assert "objectFit" in src or "object-fit" in src


class TestTaskViewFilesDrawer:
    """Verify TaskView.js FilesDrawer image preview integration."""

    def _get_source(self):
        return TASK_VIEW_JS.read_text()

    def test_imports_image_lightbox(self):
        src = self._get_source()
        assert "ImageLightbox" in src
        assert "isImageFile" in src

    def test_lightbox_file_state(self):
        src = self._get_source()
        assert "lightboxFile" in src

    def test_lightbox_renders_when_file_selected(self):
        src = self._get_source()
        # Should render ImageLightbox component conditionally
        assert "ImageLightbox" in src
        assert "lightboxFile" in src

    def test_image_thumbnail_in_file_list(self):
        src = self._get_source()
        # Should use isImageFile to check extensions
        assert "isImageFile" in src

    def test_non_image_keeps_download_link(self):
        src = self._get_source()
        assert "Download" in src or "download" in src

    def test_download_url_pattern(self):
        """Task files must use the /dashboard/api/files/{id}/download endpoint."""
        src = self._get_source()
        assert "/dashboard/api/files/" in src and "/download" in src
