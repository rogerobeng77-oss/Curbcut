import re
from dataclasses import dataclass
from pathlib import Path

from app.scanner import Violation

SKIP_DIRS = {"node_modules", ".git", "dist", "build", "vendor", "__pycache__"}
SOURCE_SUFFIXES = {".html", ".htm", ".jsx", ".tsx", ".vue", ".svelte", ".css", ".scss"}

_ATTR = re.compile(r'(src|href|id|class|name)="([^"]+)"')


@dataclass(frozen=True)
class SourceMatch:
    path: str
    line: int
    text: str


def _candidate_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def _needles(violation: Violation) -> list[str]:
    """Ordered from most to least specific."""
    needles = [violation.html.strip()]
    if violation.rule == "color-contrast":
        for name, value in _ATTR.findall(violation.html):
            if name == "class":
                needles.extend(f".{token}" for token in value.split())
    for name, value in _ATTR.findall(violation.html):
        if name in {"src", "href", "id"}:
            needles.append(f'{name}="{value}"')
    return needles


def locate(violation: Violation, root: str) -> SourceMatch | None:
    root_path = Path(root)
    for needle in _needles(violation):
        for path in _candidate_files(root_path):
            for index, line in enumerate(path.read_text(errors="ignore").splitlines(), start=1):
                if needle in line:
                    return SourceMatch(
                        path=str(path.relative_to(root_path)), line=index, text=line
                    )
    return None
