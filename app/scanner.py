import json
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import sync_playwright

from substrate.telemetry import span

# The rules the remediation loop knows how to patch. This is a *loop* filter,
# applied in app/runner.py -- deliberately NOT a scan filter. scan_page returns
# every violation axe reports, because verify() and the runner's final gate
# compare whole-page scans: filtering here would have hidden a patch that fixes
# its target while introducing, say, heading-order or duplicate-id.
SUPPORTED_RULES = frozenset(
    {"image-alt", "button-name", "link-name", "color-contrast", "label"}
)

# Pinned to 4.13.0 rather than an older minor: this project's whole pitch is
# WCAG detection, so a stale rule set is weaker detection for no benefit.
_AXE_URL = "https://cdn.jsdelivr.net/npm/axe-core@4.13.0/axe.min.js"
_AXE_CACHE = Path(__file__).parent / "_axe.min.js"


@dataclass(frozen=True)
class Violation:
    rule: str
    selector: str
    html: str
    impact: str
    description: str


def _axe_source() -> str:
    """Vendor axe on first use so scans work without network access."""
    if not _AXE_CACHE.exists():
        with urllib.request.urlopen(_AXE_URL, timeout=60) as response:
            _AXE_CACHE.write_bytes(response.read())
    return _AXE_CACHE.read_text()


def scan_page(url: str) -> tuple[list[Violation], bytes]:
    """Every violation axe reports, unfiltered, plus a full-page screenshot."""
    with span("a11y.scan", url=url):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(url, wait_until="networkidle")
            page.add_script_tag(content=_axe_source())
            raw = page.evaluate("async () => JSON.stringify(await axe.run())")
            screenshot = page.screenshot(full_page=True)
            browser.close()

    violations = []
    for entry in json.loads(raw)["violations"]:
        for node in entry["nodes"]:
            violations.append(
                Violation(
                    rule=entry["id"],
                    selector=node["target"][0] if node["target"] else "",
                    html=node["html"],
                    impact=entry.get("impact") or "unknown",
                    description=entry["description"],
                )
            )
    return violations, screenshot
