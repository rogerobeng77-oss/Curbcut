import json

from app.patcher import Patch
from app.runner import run_remediation
from app.scanner import Violation
from app.verifier import Verdict
from substrate.config import load_config
from substrate.fakes import FakeModel
from substrate.store import Store
from substrate.fakes import FakeFirestore

VIOLATION = Violation(
    rule="image-alt",
    selector=".logo",
    html='<img src="logo.png">',
    impact="critical",
    description="Images must have alternate text",
)


def _store() -> Store:
    return Store(load_config(prefix="a11y"), client=FakeFirestore())


def _reply(old: str, new: str) -> str:
    return json.dumps({"old": old, "new": new, "rationale": "added alt text"})


def _scans(*pages):
    """A scanner whose page changes between calls: call N returns page N, the
    last page repeating. A fake that returns the same violations forever says
    the patch did nothing, which is not the scenario these tests describe."""
    state = {"call": 0}

    def scan(url):
        page = pages[min(state["call"], len(pages) - 1)]
        state["call"] += 1
        return (list(page), b"png")

    return scan


def _setup(tmp_path):
    (tmp_path / "index.html").write_text('<div>\n  <img src="logo.png">\n</div>\n')
    return str(tmp_path)


def test_verified_patch_is_kept(tmp_path):
    root = _setup(tmp_path)
    model = FakeModel([_reply('  <img src="logo.png">', '  <img src="logo.png" alt="Logo">')])
    result = run_remediation(
        model, _store(), "run-1", root, "http://x",
        scan=_scans([VIOLATION], []),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )
    assert len(result.verified) == 1
    assert result.triaged == []
    assert result.safe_to_ship


def test_unresolved_patch_is_reverted_and_triaged(tmp_path):
    root = _setup(tmp_path)
    original = (tmp_path / "index.html").read_bytes()
    model = FakeModel([_reply('  <img src="logo.png">', '  <img src="logo.png" alt="Logo">')])
    result = run_remediation(
        model, _store(), "run-2", root, "http://x",
        scan=lambda url: ([VIOLATION], b"png"),
        verifier=lambda url, target, baseline, scan=None: Verdict.UNRESOLVED,
    )
    assert result.verified == []
    assert result.triaged[0]["rule"] == "image-alt"
    assert result.triaged[0]["reason"] == "unresolved"
    assert (tmp_path / "index.html").read_bytes() == original


def test_regression_is_reverted_and_triaged(tmp_path):
    root = _setup(tmp_path)
    original = (tmp_path / "index.html").read_bytes()
    model = FakeModel([_reply('  <img src="logo.png">', '  <img src="logo.png" alt="Logo">')])
    result = run_remediation(
        model, _store(), "run-3", root, "http://x",
        scan=lambda url: ([VIOLATION], b"png"),
        verifier=lambda url, target, baseline, scan=None: Verdict.REGRESSED,
    )
    assert result.verified == []
    assert result.triaged[0]["reason"] == "regressed"
    assert (tmp_path / "index.html").read_bytes() == original


def test_malformed_model_reply_is_triaged_not_crashed(tmp_path):
    root = _setup(tmp_path)
    result = run_remediation(
        model=FakeModel(["I think you should add alt text"]),
        store=_store(), run_id="run-4", root=root, url="http://x",
        scan=lambda url: ([VIOLATION], b"png"),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )
    assert result.verified == []
    assert result.triaged[0]["reason"] == "no_patch"


def test_run_writes_an_audit_trail(tmp_path):
    root = _setup(tmp_path)
    store = _store()
    model = FakeModel([_reply('  <img src="logo.png">', '  <img src="logo.png" alt="Logo">')])
    run_remediation(
        model, store, "run-5", root, "http://x",
        scan=_scans([VIOLATION], []),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )
    steps = [entry["step"] for entry in store.audit_trail("run-5")]
    assert steps == ["scan", "locate", "propose", "apply", "verify", "final_scan"]


def test_INVARIANT_every_returned_patch_was_verified_resolved(tmp_path):
    """CORE INVARIANT — do not weaken. No unverified change may be returned."""
    root = _setup(tmp_path)
    seen = []

    def recording_verifier(url, target, baseline, scan=None):
        seen.append(target)
        return Verdict.UNRESOLVED

    model = FakeModel([_reply('  <img src="logo.png">', '  <img src="logo.png" alt="Logo">')])
    result = run_remediation(
        model, _store(), "run-6", root, "http://x",
        scan=lambda url: ([VIOLATION], b"png"),
        verifier=recording_verifier,
    )
    assert seen, "verifier must be called for every proposed patch"
    assert result.verified == [], "an unresolved patch must never be returned as verified"


def test_not_located_is_triaged_not_crashed(tmp_path):
    """A violation whose markup isn't found in any source file must be
    triaged, not passed on to propose_patch as a None match."""
    root = _setup(tmp_path)
    ghost = Violation(
        rule="image-alt",
        selector=".ghost",
        html='<img src="ghost.png">',
        impact="critical",
        description="d",
    )
    result = run_remediation(
        FakeModel([]), _store(), "run-7", root, "http://x",
        scan=lambda url: ([ghost], b"png"),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )
    assert result.verified == []
    assert result.triaged[0]["reason"] == "not_located"


def test_apply_failure_is_triaged_not_crashed(tmp_path, monkeypatch):
    """If apply_patch reports failure (e.g. the source line moved under it),
    the run must triage, not silently keep going or crash."""
    root = _setup(tmp_path)
    model = FakeModel([_reply('  <img src="logo.png">', '  <img src="logo.png" alt="Logo">')])
    monkeypatch.setattr("app.runner.apply_patch", lambda patch, root: False)
    result = run_remediation(
        model, _store(), "run-8", root, "http://x",
        scan=lambda url: ([VIOLATION], b"png"),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )
    assert result.verified == []
    assert result.triaged[0]["reason"] == "apply_failed"


# --- Finding 1: the final whole-page gate --------------------------------

MAP_LINE = '    <a href="/map"><img src="map-thumb.png"></a>'
MAP_IMAGE_ALT = Violation(
    rule="image-alt",
    selector='img[src$="map-thumb.png"]',
    html='<img src="map-thumb.png">',
    impact="critical",
    description="Images must have alternate text",
)
MAP_LINK_NAME = Violation(
    rule="link-name",
    selector='a[href$="map"]',
    html='<a href="/map"><img src="map-thumb.png"></a>',
    impact="serious",
    description="Links must have discernible text",
)


def test_REVIEWER_two_patches_one_line_no_longer_returns_two_verified(tmp_path):
    """fixture/index.html:21 fires image-alt and link-name on one source line.

    The image-alt patch adds alt=""; the link-name patch -- ordinary model
    output, shown only the current line -- rewrites the same line to use
    aria-label and drops the alt. Per-violation verify clears both: the
    returning image-alt is in the frozen baseline, so it is never "new".
    Before the final gate this run returned two verified patches with
    image-alt still live on the page.
    """
    (tmp_path / "index.html").write_text(f"<main>\n{MAP_LINE}\n</main>\n")
    fixed_alt = '    <a href="/map"><img src="map-thumb.png" alt="Trail map"></a>'
    aria_label = '    <a href="/map" aria-label="Trail map"><img src="map-thumb.png"></a>'
    model = FakeModel([_reply(MAP_LINE, fixed_alt), _reply(fixed_alt, aria_label)])

    result = run_remediation(
        model, _store(), "run-reviewer", str(tmp_path), "http://x",
        # real verify(), driven by a page that behaves exactly as the browser
        # does: alt fixes both rules, aria-label re-breaks image-alt.
        scan=_scans([MAP_IMAGE_ALT, MAP_LINK_NAME], [], [MAP_IMAGE_ALT], [MAP_IMAGE_ALT]),
    )

    assert [patch.new for patch in result.verified] == [aria_label]
    assert [(t["rule"], t["reason"]) for t in result.triaged] == [
        ("image-alt", "final_scan_unresolved")
    ]
    assert result.reappeared == [
        {"rule": "image-alt", "selector": 'img[src$="map-thumb.png"]', "seen": 1, "expected": 0}
    ]
    assert not result.safe_to_ship
    assert result.tree_modified, "the superseded image-alt patch could not be reverted"


def test_final_gate_catches_a_new_violation_at_a_baseline_identity(tmp_path):
    """Same rule, same selector, different node: invisible to verify's set
    comparison, caught by the gate's per-identity counts."""
    root = _setup(tmp_path)
    twin = Violation(
        rule=VIOLATION.rule,
        selector=VIOLATION.selector,
        html='<img src="other.png">',
        impact="critical",
        description="d",
    )
    model = FakeModel([_reply('  <img src="logo.png">', '  <img src="logo.png" alt="Logo">')])
    result = run_remediation(
        model, _store(), "run-twin", root, "http://x",
        scan=_scans([VIOLATION], [twin]),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )
    assert result.verified == []
    assert result.triaged[0]["reason"] == "final_scan_unresolved"
    assert not result.safe_to_ship


def test_final_scan_failure_returns_nothing_as_verified(tmp_path):
    """No final gate, no output: a run that cannot re-scan cannot vouch for
    anything, so the candidate patch is reverted and triaged."""
    root = _setup(tmp_path)
    original = (tmp_path / "index.html").read_bytes()

    def scan(url, state={"call": 0}):
        state["call"] += 1
        if state["call"] > 1:
            raise TimeoutError("playwright: navigation timeout")
        return ([VIOLATION], b"png")

    model = FakeModel([_reply('  <img src="logo.png">', '  <img src="logo.png" alt="Logo">')])
    result = run_remediation(
        model, _store(), "run-final-fail", root, "http://x",
        scan=scan,
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )
    assert result.verified == []
    assert result.final_scan_ok is False
    assert result.triaged[0]["reason"] == "final_scan_failed"
    assert result.triaged[0]["reverted"] is True
    assert not result.safe_to_ship
    assert (tmp_path / "index.html").read_bytes() == original


def test_unsupported_rule_is_triaged_and_never_patched(tmp_path):
    """scan_page no longer filters, so the loop must: a rule it has no patch
    strategy for goes to a human instead of the model."""
    root = _setup(tmp_path)
    heading = Violation(
        rule="heading-order",
        selector="h3",
        html="<h3>x</h3>",
        impact="moderate",
        description="Heading levels should only increase by one",
    )
    result = run_remediation(
        FakeModel([]), _store(), "run-unsupported", root, "http://x",
        scan=_scans([heading], []),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )
    assert result.verified == []
    assert result.triaged == [{"rule": "heading-order", "reason": "unsupported_rule"}]
    assert result.safe_to_ship
