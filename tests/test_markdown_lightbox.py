"""Tests for markdown preview lightbox feature.

Covers:
- isMarkdownFile extension detection logic (verified via JS source content)
- MarkdownLightbox component structure and highlight.js integration
- Files page markdown wiring
"""

import re
from pathlib import Path

DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"
LIGHTBOX_JS = DASHBOARD_DIR / "components" / "MarkdownLightbox.js"
FILES_JS = DASHBOARD_DIR / "components" / "Files.js"


class TestIsMarkdownFileDetection:
    """Verify isMarkdownFile function covers all required extensions."""

    def _get_source(self):
        return LIGHTBOX_JS.read_text()

    def test_lightbox_file_exists(self):
        assert LIGHTBOX_JS.exists(), "MarkdownLightbox.js component file must exist"

    def test_is_markdown_file_function_exported(self):
        src = self._get_source()
        assert "export function isMarkdownFile" in src

    def test_detects_md(self):
        src = self._get_source()
        match = re.search(r"function isMarkdownFile\(.+?\{(.+?)\}", src, re.DOTALL)
        assert match, "isMarkdownFile function body not found"
        assert "md" in match.group(1)

    def test_detects_markdown(self):
        src = self._get_source()
        match = re.search(r"function isMarkdownFile\(.+?\{(.+?)\}", src, re.DOTALL)
        assert match, "isMarkdownFile function body not found"
        assert "markdown" in match.group(1)

    def test_all_markdown_extensions_in_array(self):
        """Both 'md' and 'markdown' must appear in the isMarkdownFile function."""
        src = self._get_source()
        match = re.search(r"function isMarkdownFile\(.+?\{(.+?)\}", src, re.DOTALL)
        assert match, "isMarkdownFile function body not found"
        body = match.group(1)
        for ext in ["md", "markdown"]:
            assert ext in body, f"Extension '{ext}' not found in isMarkdownFile"

    def test_non_markdown_extensions_not_in_array(self):
        """Confirm js/py/txt are not whitelisted as markdown extensions."""
        src = self._get_source()
        match = re.search(r"function isMarkdownFile\(.+?\{(.+?)\}", src, re.DOTALL)
        assert match
        body = match.group(1)
        for ext in ["js", "py", "txt", "html"]:
            assert ext not in body, f"Non-markdown extension '{ext}' should not be in isMarkdownFile"


class TestMarkdownLightboxComponent:
    """Verify MarkdownLightbox component structure."""

    def _get_source(self):
        return LIGHTBOX_JS.read_text()

    def test_component_exported(self):
        src = self._get_source()
        assert "export function MarkdownLightbox" in src

    def test_accepts_src_prop(self):
        src = self._get_source()
        assert "src" in src

    def test_accepts_filename_prop(self):
        src = self._get_source()
        assert "filename" in src

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

    def test_click_backdrop_to_close(self):
        """Backdrop onClick should call onClose."""
        src = self._get_source()
        assert "onClick" in src and "onClose" in src

    def test_content_container_max_width_800(self):
        src = self._get_source()
        assert "800px" in src, "Content container must have max-width: 800px"

    def test_uses_dompurify(self):
        src = self._get_source()
        assert "DOMPurify" in src, "Content must be sanitized with DOMPurify"

    def test_uses_marked_parse(self):
        src = self._get_source()
        assert "marked.parse" in src, "Content must be parsed with marked.parse()"

    def test_has_loading_state(self):
        src = self._get_source()
        assert "Loading" in src or "loading" in src

    def test_has_error_state(self):
        src = self._get_source()
        assert "error" in src.lower() and ("Failed" in src or "failed" in src)

    def test_fetches_from_src(self):
        src = self._get_source()
        assert "fetch(src)" in src, "Component must fetch file content from src URL"


class TestHighlightJsIntegration:
    """Verify highlight.js is imported and all required languages are registered."""

    def _get_source(self):
        return LIGHTBOX_JS.read_text()

    def test_hljs_core_imported(self):
        src = self._get_source()
        assert "highlight.js" in src and "/lib/core" in src

    def test_registers_javascript(self):
        src = self._get_source()
        assert "registerLanguage('javascript'" in src

    def test_registers_typescript(self):
        src = self._get_source()
        assert "registerLanguage('typescript'" in src

    def test_registers_python(self):
        src = self._get_source()
        assert "registerLanguage('python'" in src

    def test_registers_bash(self):
        src = self._get_source()
        assert "registerLanguage('bash'" in src

    def test_registers_json(self):
        src = self._get_source()
        assert "registerLanguage('json'" in src

    def test_registers_html_or_xml(self):
        src = self._get_source()
        assert "registerLanguage('html'" in src or "registerLanguage('xml'" in src

    def test_registers_css(self):
        src = self._get_source()
        assert "registerLanguage('css'" in src

    def test_registers_sql(self):
        src = self._get_source()
        assert "registerLanguage('sql'" in src

    def test_registers_yaml(self):
        src = self._get_source()
        assert "registerLanguage('yaml'" in src

    def test_registers_markdown(self):
        src = self._get_source()
        assert "registerLanguage('markdown'" in src

    def test_registers_php(self):
        src = self._get_source()
        assert "registerLanguage('php'" in src

    def test_registers_ruby(self):
        src = self._get_source()
        assert "registerLanguage('ruby'" in src

    def test_registers_go(self):
        src = self._get_source()
        assert "registerLanguage('go'" in src

    def test_registers_rust(self):
        src = self._get_source()
        assert "registerLanguage('rust'" in src

    def test_registers_diff(self):
        src = self._get_source()
        assert "registerLanguage('diff'" in src

    def test_marked_configured_with_hljs(self):
        src = self._get_source()
        assert "marked.setOptions" in src or "marked.use" in src
        assert "hljs" in src

    def test_syntax_styles_scoped_to_md_lightbox_content(self):
        src = self._get_source()
        assert ".md-lightbox-content .hljs" in src, "Syntax highlight styles must be scoped to .md-lightbox-content"

    def test_code_block_dark_background(self):
        src = self._get_source()
        assert "#0d0b09" in src, "Code blocks must use the Copper Forge terminal dark background"


class TestFilesPageMarkdownIntegration:
    """Verify Files.js is wired to open the MarkdownLightbox for .md files."""

    def _get_source(self):
        return FILES_JS.read_text()

    def test_files_js_imports_markdown_lightbox(self):
        src = self._get_source()
        assert "MarkdownLightbox" in src
        assert "MarkdownLightbox.js" in src

    def test_files_js_imports_is_markdown_file(self):
        src = self._get_source()
        assert "isMarkdownFile" in src

    def test_markdown_files_use_accent_color(self):
        src = self._get_source()
        # Markdown filenames should be styled with accent color (same as images)
        assert "isMarkdown" in src
        assert "accent" in src

    def test_markdown_files_use_pointer_cursor(self):
        src = self._get_source()
        assert "isMarkdown" in src
        assert "pointer" in src

    def test_markdown_lightbox_rendered_conditionally(self):
        src = self._get_source()
        assert "MarkdownLightbox" in src
        assert "isMarkdown" in src

    def test_markdown_lightbox_receives_src(self):
        src = self._get_source()
        # The MarkdownLightbox should receive a src prop (download URL)
        match = re.search(r"MarkdownLightbox.+?onClose", src, re.DOTALL)
        assert match, "MarkdownLightbox usage not found"
        block = match.group(0)
        assert "src" in block

    def test_markdown_lightbox_receives_filename(self):
        src = self._get_source()
        match = re.search(r"MarkdownLightbox.+?onClose", src, re.DOTALL)
        assert match, "MarkdownLightbox usage not found"
        block = match.group(0)
        assert "filename" in block

    def test_markdown_click_opens_lightbox(self):
        src = self._get_source()
        # Clicking a markdown filename should set lightbox state to true
        assert "isMarkdown" in src
        assert "setLightbox(true)" in src
