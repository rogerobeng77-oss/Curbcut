import json

from app.locator import SourceMatch
from app.patcher import Patch, propose_patch
from app.scanner import Violation
from substrate.fakes import FakeModel

VIOLATION = Violation(
    rule="image-alt",
    selector=".logo",
    html='<img src="logo.png" class="logo">',
    impact="critical",
    description="Images must have alternate text",
)
MATCH = SourceMatch(path="index.html", line=2, text='  <img src="logo.png" class="logo">')


def _reply(**overrides) -> str:
    payload = {
        "old": '  <img src="logo.png" class="logo">',
        "new": '  <img src="logo.png" class="logo" alt="Northwind Parks logo">',
        "rationale": "Added a descriptive alt attribute naming the organisation.",
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_returns_patch_from_valid_model_reply():
    patch = propose_patch(FakeModel([_reply()]), VIOLATION, MATCH, screenshot=b"png")
    assert isinstance(patch, Patch)
    assert patch.path == "index.html"
    assert patch.line == 2
    assert 'alt="Northwind Parks logo"' in patch.new
    assert patch.rationale


def test_passes_screenshot_to_the_model():
    model = FakeModel([_reply()])
    propose_patch(model, VIOLATION, MATCH, screenshot=b"png-bytes")
    assert model.calls[0]["images"] == [b"png-bytes"]


def test_prompt_includes_rule_source_line_and_selector():
    model = FakeModel([_reply()])
    propose_patch(model, VIOLATION, MATCH, screenshot=b"png")
    prompt = model.calls[0]["prompt"]
    assert "image-alt" in prompt
    assert '<img src="logo.png" class="logo">' in prompt
    assert ".logo" in prompt


def test_returns_none_on_non_json_reply():
    assert propose_patch(FakeModel(["I suggest adding alt text."]), VIOLATION, MATCH, b"p") is None


def test_returns_none_when_old_does_not_match_source_line():
    reply = _reply(old='<img src="different.png">')
    assert propose_patch(FakeModel([reply]), VIOLATION, MATCH, b"p") is None


def test_returns_none_when_patch_is_a_noop():
    reply = _reply(new='  <img src="logo.png" class="logo">')
    assert propose_patch(FakeModel([reply]), VIOLATION, MATCH, b"p") is None


def test_strips_markdown_fences_around_json():
    fenced = "```json\n" + _reply() + "\n```"
    assert propose_patch(FakeModel([fenced]), VIOLATION, MATCH, b"p") is not None


def test_returns_none_when_replacement_spans_two_lines():
    """The canonical `label` fix is to add a <label> element, and a model that
    writes it on its own line produces a patch app/applier.py cannot revert:
    the file grows a line, revert finds a mismatch, and the rejected edit stays
    on disk for `git commit -am` to sweep into the PR."""
    reply = _reply(
        new='  <label for="notify">Email</label>\n  <input type="email" id="notify">'
    )
    assert propose_patch(FakeModel([reply]), VIOLATION, MATCH, b"p") is None


def test_returns_none_for_exotic_line_separators():
    """str.splitlines() splits on more than \\n, and app/applier.py uses it."""
    for separator in ("\r", "\u2028", "\x0b", "\x85"):
        reply = _reply(new=f'  <img src="logo.png">{separator}  <p>x</p>')
        assert propose_patch(FakeModel([reply]), VIOLATION, MATCH, b"p") is None, separator


def test_propose_patch_requests_json_mode():
    """The schema must reach the model, not just sit in a constant.

    Without it the adapter falls back to free-form text, which is exactly the
    failure this was written to close: three of seven patches rejected as
    `not_json` in a real run against an unchanged fixture.
    """
    from app.patcher import PATCH_SCHEMA, propose_patch
    from app.scanner import Violation
    from app.locator import SourceMatch
    from substrate.fakes import FakeModel

    model = FakeModel(['{"old": "<img src=\\"a.png\\">", '
                       '"new": "<img src=\\"a.png\\" alt=\\"A\\">", '
                       '"rationale": "adds a name"}'])
    violation = Violation(rule="image-alt", selector="img", html="<img src='a.png'>",
                          impact="critical", description="Images must have alternate text")
    match = SourceMatch(path="index.html", line=1, text='<img src="a.png">')

    patch = propose_patch(model, violation, match, b"")

    assert patch is not None
    assert model.calls[0]["response_schema"] == PATCH_SCHEMA
    assert model.calls[0]["response_schema"]["required"] == ["old", "new", "rationale"]
