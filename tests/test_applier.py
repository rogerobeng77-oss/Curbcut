from pathlib import Path

from app.applier import apply_patch, revert_patch
from app.patcher import Patch

PATCH = Patch(
    path="index.html",
    line=2,
    old='  <img src="logo.png">',
    new='  <img src="logo.png" alt="Logo">',
    rationale="added alt",
)


def _seed(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text('<div>\n  <img src="logo.png">\n</div>\n')


def test_apply_replaces_the_target_line(tmp_path: Path):
    _seed(tmp_path)
    assert apply_patch(PATCH, str(tmp_path)) is True
    assert (tmp_path / "index.html").read_text().splitlines()[1] == PATCH.new


def test_apply_preserves_surrounding_lines(tmp_path: Path):
    _seed(tmp_path)
    apply_patch(PATCH, str(tmp_path))
    lines = (tmp_path / "index.html").read_text().splitlines()
    assert lines[0] == "<div>"
    assert lines[2] == "</div>"


def test_apply_returns_false_when_line_does_not_match(tmp_path: Path):
    (tmp_path / "index.html").write_text("<div>\n  <p>changed</p>\n</div>\n")
    assert apply_patch(PATCH, str(tmp_path)) is False


def test_apply_returns_false_when_file_missing(tmp_path: Path):
    assert apply_patch(PATCH, str(tmp_path)) is False


def test_revert_restores_the_original_line(tmp_path: Path):
    _seed(tmp_path)
    apply_patch(PATCH, str(tmp_path))
    assert revert_patch(PATCH, str(tmp_path)) is True
    assert (tmp_path / "index.html").read_text().splitlines()[1] == PATCH.old


def test_apply_then_revert_is_byte_identical(tmp_path: Path):
    _seed(tmp_path)
    before = (tmp_path / "index.html").read_bytes()
    apply_patch(PATCH, str(tmp_path))
    revert_patch(PATCH, str(tmp_path))
    assert (tmp_path / "index.html").read_bytes() == before
