import http.server
import socketserver
import threading
from contextlib import contextmanager

import pytest

from app.scanner import SUPPORTED_RULES, Violation, scan_page


@contextmanager
def serve(directory: str):
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=directory, **kw
    )
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{httpd.server_address[1]}/index.html"
        finally:
            httpd.shutdown()


def test_supported_rules_are_the_four_families():
    assert SUPPORTED_RULES == frozenset(
        {"image-alt", "button-name", "link-name", "color-contrast", "label"}
    )


@pytest.mark.slow
def test_scan_finds_seeded_violations_and_returns_screenshot():
    with serve("fixture") as url:
        violations, screenshot = scan_page(url)
    rules = {v.rule for v in violations}
    assert "image-alt" in rules
    assert "color-contrast" in rules
    assert all(isinstance(v, Violation) for v in violations)
    assert screenshot[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.slow
def test_scan_populates_selector_and_html():
    with serve("fixture") as url:
        violations, _ = scan_page(url)
    image_alt = next(v for v in violations if v.rule == "image-alt")
    assert image_alt.selector
    assert "<img" in image_alt.html
