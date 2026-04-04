#!/usr/bin/env python3
"""Take screenshots for index search snippets and TOC accent bar."""

import json
import socket
import threading
import time
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

CONVERSATIONS_FIXTURE = json.loads(
    (REPO / "fixtures/visual/project-conversations.json").read_text()
)

PROJECT_FIXTURE = {
    "id": "mcp-switchboard",
    "name": "mcp-switchboard",
    "repo_url": "https://github.com/example/mcp-switchboard",
    "test_command": "pytest",
    "model": "sonnet",
    "status": "active",
    "created_at": "2026-03-01T00:00:00Z",
    "updated_at": "2026-04-03T00:00:00Z",
}

INDEX_SEARCH_FIXTURE = {
    "results": [
        {
            "conversation_id": "foreman-design",
            "message_id": "msg-001",
            "title": "Foreman dashboard design system and UI spec",
            "snippet": "Design tokens: Copper Forge amber palette for accent colours across all components.",
            "message_type": "spec",
            "relevance_score": 0.95,
        },
        {
            "conversation_id": "auth-refactor-plan",
            "message_id": "msg-002",
            "title": "Plan the OAuth 2.0 migration and session handling rewrite",
            "snippet": "Using RS256 JWTs with jti revocation. PKCE S256 support for all clients.",
            "message_type": "plan",
            "relevance_score": 0.88,
        },
        {
            "conversation_id": "gate-pipeline-design",
            "message_id": "msg-003",
            "title": "Test gate and review gate pipeline design",
            "snippet": "Test gate runs pytest, review gate uses Opus model. Both retry on failure.",
            "message_type": "result",
            "relevance_score": 0.75,
        },
        {
            "conversation_id": "embedding-search",
            "message_id": "msg-004",
            "title": "Semantic search with OpenAI embeddings — design and implementation notes",
            "snippet": "OpenAI text-embedding-3-small model. Cosine similarity for ranking. Chunks stored per message.",
            "message_type": "note",
            "relevance_score": 0.62,
        },
    ],
    "total": 4,
}

CONVERSATION_FIXTURE = json.loads(
    (REPO / "fixtures/visual/conversation-detail.json").read_text()
)


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
    index_url = f"{base}/dashboard#/project/mcp-switchboard/conversations"
    conv_url = f"{base}/dashboard#/project/mcp-switchboard/conversation/foreman-design"

    project_body = json.dumps(PROJECT_FIXTURE)
    convs_body = json.dumps(CONVERSATIONS_FIXTURE)
    search_body = json.dumps(INDEX_SEARCH_FIXTURE)
    conv_body = json.dumps(CONVERSATION_FIXTURE)
    empty = json.dumps([])

    with sync_playwright() as p:
        browser = p.chromium.launch()

        # ── Screenshot 1: Index search with snippet cards ──
        ctx1 = browser.new_context(viewport={"width": 1280, "height": 900})
        page1 = ctx1.new_page()
        # Fallback first (lower priority), specific routes last (LIFO)
        page1.route("**/dashboard/api/**",
                    lambda r: r.fulfill(status=200, content_type="application/json", body='{}'))
        page1.route("**/dashboard/api/tasks**",
                    lambda r: r.fulfill(status=200, content_type="application/json", body=empty))
        page1.route("**/dashboard/api/conversations**",
                    lambda r: r.fulfill(status=200, content_type="application/json", body=convs_body))
        page1.route("**/dashboard/api/projects/mcp-switchboard",
                    lambda r: r.fulfill(status=200, content_type="application/json", body=project_body))
        page1.route("**/dashboard/api/search**",
                    lambda r: r.fulfill(status=200, content_type="application/json", body=search_body))
        page1.goto(index_url, wait_until="networkidle")
        page1.wait_for_timeout(1500)
        # Type into the search box to trigger search
        page1.fill("input[placeholder*='Search']", "design")
        page1.wait_for_timeout(600)
        out1 = "/tmp/conv-index-search-snippets.png"
        page1.screenshot(path=out1, full_page=False)
        print(f"Screenshot 1 saved: {out1}")
        ctx1.close()

        # ── Screenshot 2: Desktop TOC with accent bar on active item ──
        ctx2 = browser.new_context(viewport={"width": 1280, "height": 900})
        page2 = ctx2.new_page()
        # Fallback first, specific last
        page2.route("**/dashboard/api/**",
                    lambda r: r.fulfill(status=200, content_type="application/json", body='{}'))
        page2.route("**/dashboard/api/conversations/foreman-design",
                    lambda r: r.fulfill(status=200, content_type="application/json", body=conv_body))
        page2.goto(conv_url, wait_until="networkidle")
        page2.wait_for_timeout(2000)
        # Scroll down a bit so the scroll spy marks a message as active
        page2.evaluate("window.scrollBy(0, 400)")
        page2.wait_for_timeout(500)
        out2 = "/tmp/conv-toc-accent-bar.png"
        page2.screenshot(path=out2, full_page=False)
        print(f"Screenshot 2 saved: {out2}")
        ctx2.close()

        browser.close()

    server.shutdown()
    return out1, out2


if __name__ == "__main__":
    out1, out2 = main()
    print("Done.")
    print(f"  Index search snippets: {out1}")
    print(f"  TOC accent bar:        {out2}")
