#!/usr/bin/env python3
"""Visual check tool — takes Playwright screenshots of dashboard pages with mock data.

Usage:
    python3 scripts/visual-check.py settings              # desktop settings
    python3 scripts/visual-check.py settings-mobile        # mobile settings
    python3 scripts/visual-check.py landing                # projects page
    python3 scripts/visual-check.py settings settings-mobile  # multiple pages
"""

import json
import re
import socket
import sys
import threading
import time
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "visual-config.json"


def load_config(config_path=None):
    """Load and return the visual check configuration."""
    path = Path(config_path) if config_path else CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path) as f:
        return json.load(f)


def load_fixture(fixture_path, base_dir):
    """Load a mock fixture file and return its contents as a string."""
    full_path = base_dir / fixture_path
    if not full_path.exists():
        raise FileNotFoundError(f"Fixture file not found: {full_path}")
    return full_path.read_text()


def find_free_port():
    """Find and return a free port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ForemanHandler(SimpleHTTPRequestHandler):
    """HTTP handler that serves foreman.html for /foreman requests."""

    def translate_path(self, path):
        # Strip query string and fragment for path resolution
        path = path.split("?")[0].split("#")[0]
        if path == "/foreman" or path == "/foreman/":
            path = "/foreman.html"
        return super().translate_path(path)

    def log_message(self, format, *args):
        # Silence HTTP server logs
        pass


def start_server(serve_dir, port):
    """Start an HTTP server in a daemon thread. Returns the server instance."""
    handler = partial(ForemanHandler, directory=serve_dir)
    server = HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def setup_mock_routes(page, mocks, base_dir):
    """Set up Playwright route interception for mock API responses."""
    for api_path, fixture_path in mocks.items():
        fixture_content = load_fixture(fixture_path, base_dir)
        content_to_capture = fixture_content  # capture for closure

        def make_handler(body):
            def handler(route):
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=body,
                )
            return handler

        # Use regex matching for paths with encoded characters (%2F etc.)
        if "%" in api_path:
            escaped = re.escape(api_path).replace(r"\%", "%")
            page.route(re.compile(escaped), make_handler(content_to_capture))
        else:
            page.route(f"**{api_path}", make_handler(content_to_capture))


def screenshot_page(browser, page_name, page_config, base_url, base_dir):
    """Take a screenshot of a single page. Returns the output path."""
    viewport = page_config["viewport"]
    context = browser.new_context(
        viewport={"width": viewport["width"], "height": viewport["height"]},
    )
    page = context.new_page()

    # Set up mock routes
    mocks = page_config.get("mocks", {})
    if mocks:
        setup_mock_routes(page, mocks, base_dir)

    # Navigate — use full URL with hash fragment
    url = base_url + page_config["url"]
    page.goto(url, wait_until="networkidle")
    wait_ms = page_config.get("wait_ms", 1500)
    time.sleep(wait_ms / 1000.0)
    # Execute any post-load clicks (e.g., expand drawers for screenshots)
    for selector in page_config.get("click", []):
        try:
            page.click(selector, timeout=3000)
            time.sleep(0.3)
        except Exception as e:
            print(f"  Click failed on '{selector}': {e}")
    wait_after_clicks_ms = page_config.get("wait_after_clicks_ms", 0)
    if wait_after_clicks_ms:
        time.sleep(wait_after_clicks_ms / 1000.0)

    # Take screenshot
    output_path = f"/tmp/visual-check-{page_name}.png"
    full_page = page_config.get("full_page", True)
    page.screenshot(path=output_path, full_page=full_page)

    context.close()
    return output_path


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/visual-check.py <page> [page2 ...]")
        print("Available pages are defined in scripts/visual-config.json")
        sys.exit(1)

    requested_pages = sys.argv[1:]

    # Load config
    config = load_config()
    pages = config.get("pages", {})

    # Validate requested pages
    invalid = [p for p in requested_pages if p not in pages]
    if invalid:
        print(f"Error: Unknown page(s): {', '.join(invalid)}", file=sys.stderr)
        print(f"Available pages: {', '.join(pages.keys())}", file=sys.stderr)
        sys.exit(1)

    # Determine base directory (worktree root)
    base_dir = (SCRIPT_DIR / "..").resolve()
    serve_dir = (base_dir / config.get("serve_dir", ".")).resolve()

    if not serve_dir.exists():
        print(f"Error: Serve directory not found: {serve_dir}", file=sys.stderr)
        sys.exit(1)

    # Start HTTP server
    port = find_free_port()
    server = start_server(str(serve_dir), port)
    base_url = f"http://127.0.0.1:{port}"

    try:
        # Launch Playwright
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print("Error: Playwright is not installed.", file=sys.stderr)
            print("Run: pip install playwright && playwright install chromium", file=sys.stderr)
            sys.exit(1)

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                if "Executable doesn't exist" in str(e) or "browserType.launch" in str(e):
                    print("Error: Chromium is not installed.", file=sys.stderr)
                    print("Run: playwright install chromium", file=sys.stderr)
                    sys.exit(1)
                raise

            try:
                output_paths = []
                for page_name in requested_pages:
                    page_config = pages[page_name]
                    path = screenshot_page(
                        browser, page_name, page_config, base_url, base_dir
                    )
                    output_paths.append(path)
                    print(f"Screenshot saved: {path}")

                # Print summary with reference info
                for page_name in requested_pages:
                    ref = pages[page_name].get("reference")
                    if ref:
                        ref_path = base_dir / ref
                        if ref_path.exists():
                            print(f"Reference for {page_name}: {ref_path}")
                        else:
                            print(f"Reference for {page_name}: (not found: {ref_path})")
            finally:
                browser.close()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
