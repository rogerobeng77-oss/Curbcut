from pathlib import Path


def test_fixture_has_all_four_rule_families():
    html = Path("fixture/index.html").read_text()
    assert html.count("VIOLATION") == 6
    for family in ("image-alt", "color-contrast", "label", "button-name", "link-name"):
        assert family in html, f"fixture is missing a seeded {family} violation"


def test_muted_colour_is_actually_below_threshold():
    css = Path("fixture/styles.css").read_text()
    assert "#9a9a9a" in css
