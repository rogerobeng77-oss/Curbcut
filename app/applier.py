from pathlib import Path

from app.patcher import Patch
from substrate.telemetry import log_event


def _swap(patch: Patch, root: str, expect: str, replace_with: str) -> bool:
    target = Path(root) / patch.path
    if not target.is_file():
        log_event("patch.apply_failed", severity="WARNING", path=patch.path, reason="missing_file")
        return False

    content = target.read_text()
    keep_trailing_newline = content.endswith("\n")
    lines = content.splitlines()
    index = patch.line - 1

    if index < 0 or index >= len(lines) or lines[index] != expect:
        log_event("patch.apply_failed", severity="WARNING", path=patch.path, reason="line_mismatch")
        return False

    lines[index] = replace_with
    target.write_text("\n".join(lines) + ("\n" if keep_trailing_newline else ""))
    return True


def apply_patch(patch: Patch, root: str) -> bool:
    return _swap(patch, root, expect=patch.old, replace_with=patch.new)


def revert_patch(patch: Patch, root: str) -> bool:
    return _swap(patch, root, expect=patch.new, replace_with=patch.old)
