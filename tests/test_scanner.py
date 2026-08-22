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


@pytest.mark.slow
def test_scan_reports_rules_outside_supported_rules(tmp_path):
    """scan_page must not filter. SUPPORTED_RULES is what the remediation loop
    can patch; if the scanner dropped everything else, verify() and the
    runner's final gate would both be blind to a patch that introduces, say,
    heading-order — and it would verify clean."""
    (tmp_path / "index.html").write_text(
        "<!doctype html><html lang=en><head><title>t</title></head>"
        "<body><main><h1>One</h1><h3>Three</h3></main></body></html>"
    )
    with serve(str(tmp_path)) as url:
        violations, _ = scan_page(url)
    rules = {v.rule for v in violations}
    assert "heading-order" in rules
    assert not rules & SUPPORTED_RULES, "this page seeds only an unsupported rule"
