from pathlib import Path

from app.locator import SourceMatch, locate
from app.scanner import Violation


def _violation(html: str, rule: str = "image-alt") -> Violation:
    return Violation(rule=rule, selector=".x", html=html, impact="serious", description="d")


def test_locates_exact_html_fragment(tmp_path: Path):
    (tmp_path / "index.html").write_text('<div>\n  <img src="logo.png" class="logo">\n</div>\n')
    match = locate(_violation('<img src="logo.png" class="logo">'), str(tmp_path))
    assert isinstance(match, SourceMatch)
    assert match.path == "index.html"
    assert match.line == 2


def test_locates_by_distinctive_attribute_when_markup_differs(tmp_path: Path):
    (tmp_path / "page.html").write_text('<img\n  src="map-thumb.png"\n  loading="lazy">\n')
    match = locate(_violation('<img src="map-thumb.png" loading="lazy">'), str(tmp_path))
    assert match.path == "page.html"
    assert match.line == 2


def test_returns_none_when_not_found(tmp_path: Path):
    (tmp_path / "index.html").write_text("<p>nothing here</p>\n")
    assert locate(_violation('<img src="absent.png">'), str(tmp_path)) is None


def test_skips_vendor_directories(tmp_path: Path):
    vendor = tmp_path / "node_modules"
    vendor.mkdir()
    (vendor / "bundle.html").write_text('<img src="logo.png">\n')
    assert locate(_violation('<img src="logo.png">'), str(tmp_path)) is None


def test_locates_contrast_violation_by_class_in_css(tmp_path: Path):
    (tmp_path / "styles.css").write_text("body { color: #000; }\n.muted { color: #9a9a9a; }\n")
    violation = _violation('<p class="muted">text</p>', rule="color-contrast")
    match = locate(violation, str(tmp_path))
    assert match.path == "styles.css"
    assert match.line == 2
