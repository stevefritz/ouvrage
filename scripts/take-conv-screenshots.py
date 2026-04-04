#!/usr/bin/env python3
"""Take conversation-view screenshots for collapsed messages and search panel."""

import json
import socket
import threading
import time
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CONVERSATION_FIXTURE = json.loads(
    (REPO / "fixtures/visual/conversation-detail.json").read_text()
)

SEARCH_RESULTS_FIXTURE = {
    "results": [
        {
            "id": "msg-004",
            "author": "cc-worker",
            "type": "result",
            "title": "Tab bar implementation complete",
            "snippet": "Implemented TabBar component with amber underline on active tab.",
            "score": 1.0,
            "created_at": "2026-03-16T14:00:00Z"
        },
        {
            "id": "msg-003",
            "author": "user",
            "type": "note",
            "title": "Tab bar spec",
            "snippet": "The tab bar should have four tabs: Tasks, Conversations, Files, Settings. Active tab gets amber underline.",
            "score": 0.9,
            "created_at": "2026-03-16T09:00:00Z"
        },
        {
            "id": "msg-008",
            "author": "cc-worker",
            "type": "result",
            "title": "Tab bar spec",
            "snippet": "Tab bar spec: four tabs: Tasks, Customization, Files, Settings. Active tab gets amber underline.",
            "score": 0.85,
            "created_at": "2026-03-19T09:30:00Z"
        }
    ],
    "total": 3
}


def find_free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ForemanHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        path = path.split("?")[0].split("#")[0]
        if path in ("/dashboard", "/dashboard/", "/foreman", "/foreman/"):
            path = "/foreman.html"
        return super().translate_path(path)

    def log_message(self, *args):
        pass


def start_server(port):
    handler = partial(ForemanHandler, directory=str(REPO))
    server = HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main():
    from playwright.sync_api import sync_playwright

    port = find_free_port()
    server = start_server(port)
    time.sleep(0.3)

    base = f"http://127.0.0.1:{port}"
    conv_url = f"{base}/dashboard#/project/mcp-switchboard/conversation/foreman-design"

    conv_body = json.dumps(CONVERSATION_FIXTURE)
    search_body = json.dumps(SEARCH_RESULTS_FIXTURE)

    with sync_playwright() as p:
        browser = p.chromium.launch()

        # ── Screenshot 1: Collapsed messages view ──
        ctx1 = browser.new_context(viewport={"width": 1280, "height": 900})
        page1 = ctx1.new_page()
        # Register general fallback FIRST (lower priority), specific routes LAST (higher priority)
        page1.route("**/dashboard/api/**",
                    lambda r: r.fulfill(status=200, content_type="application/json", body='{}'))
        page1.route("**/dashboard/api/conversations/foreman-design",
                    lambda r: r.fulfill(status=200, content_type="application/json", body=conv_body))
        page1.goto(conv_url, wait_until="networkidle")
        page1.wait_for_timeout(2000)
        out1 = "/tmp/conv-collapsed-messages.png"
        page1.screenshot(path=out1, full_page=True)
        print(f"Screenshot 1 saved: {out1}")
        ctx1.close()

        # ── Screenshot 2: Search with results panel and n/x counter ──
        ctx2 = browser.new_context(viewport={"width": 1280, "height": 900})
        page2 = ctx2.new_page()
        # Register general fallback FIRST, specific routes LAST (LIFO matching)
        page2.route("**/dashboard/api/**",
                    lambda r: r.fulfill(status=200, content_type="application/json", body='{}'))
        page2.route("**/dashboard/api/conversations/foreman-design",
                    lambda r: r.fulfill(status=200, content_type="application/json", body=conv_body))
        page2.route("**/dashboard/api/conversations/foreman-design/search*",
                    lambda r: r.fulfill(status=200, content_type="application/json", body=search_body))
        page2.goto(conv_url, wait_until="networkidle")
        page2.wait_for_timeout(2000)
        # Fill the search input
        page2.fill("input[placeholder*='Search']", "tab bar")
        page2.wait_for_timeout(800)
        out2 = "/tmp/conv-search-results.png"
        page2.screenshot(path=out2, full_page=False)
        print(f"Screenshot 2 saved: {out2}")
        ctx2.close()

        browser.close()

    server.shutdown()
    return out1, out2


if __name__ == "__main__":
    out1, out2 = main()
    print("Done.")
    print(f"  Collapsed view: {out1}")
    print(f"  Search panel:   {out2}")
