import json

import pytest

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


@pytest.mark.parametrize("verdict", [Verdict.UNRESOLVED, Verdict.REGRESSED])
def test_INVARIANT_every_returned_patch_was_verified_resolved(tmp_path, verdict):
    """CORE INVARIANT — do not weaken. No unverified change may be returned.

    Both non-resolved verdicts, not just UNRESOLVED: mutating the guard to
    `verdict is not Verdict.UNRESOLVED` accepts REGRESSED, and a test that only
    ever feeds UNRESOLVED stays green through that mutation.

    The scanner fake shows a clean page after the patch on purpose, so the
    final gate cannot cover for the loop's guard here. If the guard lets a
    non-resolved patch through, the gate sees nothing wrong with the page and
    returns it as verified — which is what this test must catch.
    """
    root = _setup(tmp_path)
    seen = []

    def recording_verifier(url, target, baseline, scan=None):
        seen.append(target)
        return verdict

    model = FakeModel([_reply('  <img src="logo.png">', '  <img src="logo.png" alt="Logo">')])
    result = run_remediation(
        model, _store(), "run-6", root, "http://x",
        scan=_scans([VIOLATION], []),
        verifier=recording_verifier,
    )
    assert seen, "verifier must be called for every proposed patch"
    assert result.verified == [], "an unverified patch must never be returned as verified"
    assert result.triaged[0]["reason"] == verdict.value


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


# --- Findings 3 and 4: revert visibility, and errors that stay local -------

HERO = Violation(
    rule="image-alt",
    selector=".hero",
    html='<img src="hero.png">',
    impact="critical",
    description="Images must have alternate text",
)
HERO_FIX = _reply('  <img src="hero.png">', '  <img src="hero.png" alt="Hero">')
LOGO_FIX = _reply('  <img src="logo.png">', '  <img src="logo.png" alt="Logo">')


def _setup_two(tmp_path):
    (tmp_path / "index.html").write_text(
        '<div>\n  <img src="logo.png">\n  <img src="hero.png">\n</div>\n'
    )
    return str(tmp_path)


def test_failed_revert_is_reported_to_the_caller(tmp_path, monkeypatch):
    """A rejected patch whose revert fails leaves the file modified. The caller
    builds a PR from that working tree, so it has to be told."""
    root = _setup(tmp_path)
    monkeypatch.setattr("app.runner.revert_patch", lambda patch, root: False)
    store = _store()
    model = FakeModel([LOGO_FIX])
    result = run_remediation(
        model, store, "run-dirty", root, "http://x",
        scan=_scans([VIOLATION], []),
        verifier=lambda url, target, baseline, scan=None: Verdict.UNRESOLVED,
    )
    assert result.triaged[0]["reverted"] is False
    assert result.tree_modified is True
    assert not result.safe_to_ship
    assert result.unreverted[0]["rule"] == "image-alt"
    revert_steps = [
        {k: v for k, v in e.items() if k != "seq"}
        for e in store.audit_trail("run-dirty")
        if e["step"] == "revert"
    ]
    assert revert_steps == [
        {"step": "revert", "rule": "image-alt", "reason": "unresolved", "reverted": False}
    ]


def test_successful_revert_is_recorded_too(tmp_path):
    root = _setup(tmp_path)
    store = _store()
    result = run_remediation(
        FakeModel([LOGO_FIX]), store, "run-clean", root, "http://x",
        scan=_scans([VIOLATION], []),
        verifier=lambda url, target, baseline, scan=None: Verdict.REGRESSED,
    )
    assert result.triaged[0]["reverted"] is True
    assert result.tree_modified is False
    assert [e["step"] for e in store.audit_trail("run-clean")][-2:] == ["revert", "final_scan"]


def test_raising_verifier_does_not_abort_the_run(tmp_path):
    """A Playwright timeout inside verify used to propagate: the caller got an
    exception instead of a RunResult, every earlier patch was discarded and the
    last one stayed on disk."""
    root = _setup_two(tmp_path)

    def verifier(url, target, baseline, scan=None):
        if target.selector == ".logo":
            raise TimeoutError("playwright: page.goto timeout 30000ms exceeded")
        return Verdict.RESOLVED

    result = run_remediation(
        FakeModel([LOGO_FIX, HERO_FIX]), _store(), "run-boom-verify", root, "http://x",
        scan=_scans([VIOLATION, HERO], [VIOLATION]),
        verifier=verifier,
    )
    assert [t["reason"] for t in result.triaged] == ["error"]
    assert result.triaged[0]["reverted"] is True
    assert [p.new for p in result.verified] == ['  <img src="hero.png" alt="Hero">']
    assert result.safe_to_ship
    lines = (tmp_path / "index.html").read_text().splitlines()
    assert lines[1] == '  <img src="logo.png">', "the failed violation's patch must be undone"


def test_raising_model_does_not_abort_the_run(tmp_path):
    """Same for a model that throws -- a 429 mid-run costs one violation, not
    the whole run."""
    root = _setup_two(tmp_path)

    class Throttled:
        def __init__(self):
            self.calls = 0

        def generate(self, prompt, images=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("vertex 429 RESOURCE_EXHAUSTED")
            return HERO_FIX

    result = run_remediation(
        Throttled(), _store(), "run-boom-model", root, "http://x",
        scan=_scans([VIOLATION, HERO], [VIOLATION]),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )
    assert result.triaged == [{"rule": "image-alt", "reason": "error"}]
    assert [p.new for p in result.verified] == ['  <img src="hero.png" alt="Hero">']
    assert result.tree_modified is False


def test_error_is_written_to_the_audit_trail(tmp_path):
    root = _setup(tmp_path)
    store = _store()

    def verifier(url, target, baseline, scan=None):
        raise TimeoutError("boom")

    run_remediation(
        FakeModel([LOGO_FIX]), store, "run-audit-err", root, "http://x",
        scan=_scans([VIOLATION], []),
        verifier=verifier,
    )
    errors = [
        {k: v for k, v in e.items() if k != "seq"}
        for e in store.audit_trail("run-audit-err")
        if e["step"] == "error"
    ]
    assert errors == [{"step": "error", "rule": "image-alt", "error": "TimeoutError"}]


# --- The recovery path itself must not raise -------------------------------


class BlowUpStore(Store):
    """A store whose audit write fails for one step, like a Firestore blip that
    happens to land on a recovery-path write. ``append_audit`` raises exactly
    what substrate/store.py documents as its retry-ceiling failure."""

    def __init__(self, boom_step: str):
        super().__init__(load_config(prefix="a11y"), client=FakeFirestore())
        self.boom_step = boom_step

    def append_audit(self, run_id, entry):
        if entry.get("step") == self.boom_step:
            raise ValueError("Failed to commit transaction in 5 attempts.")
        return super().append_audit(run_id, entry)


def _boom_on_hero(url, target, baseline, scan=None):
    if target.selector == ".hero":
        raise TimeoutError("playwright: page.goto timeout 30000ms exceeded")
    return Verdict.RESOLVED


def test_audit_failure_inside_the_reject_path_does_not_abort_the_run(tmp_path):
    """The loop's except arm calls _reject, which writes an audit entry. That
    write is a Firestore RPC and can raise -- and used to take the whole run
    with it: no RunResult, the already-verified logo patch lost, the hero patch
    left on disk."""
    root = _setup_two(tmp_path)
    store = BlowUpStore("revert")

    result = run_remediation(
        FakeModel([LOGO_FIX, HERO_FIX]), store, "run-reject-audit", root, "http://x",
        scan=_scans([VIOLATION, HERO], [HERO]),
        verifier=_boom_on_hero,
    )

    assert [p.new for p in result.verified] == ['  <img src="logo.png" alt="Logo">']
    assert result.triaged == [{"rule": "image-alt", "reason": "error", "reverted": True}]
    assert result.safe_to_ship
    lines = (tmp_path / "index.html").read_text().splitlines()
    assert lines[2] == '  <img src="hero.png">', "the failed violation's patch must be undone"


def test_a_dropped_audit_entry_is_returned_not_silently_lost(tmp_path):
    """The trail is what the demo renders, so an entry that could not be
    persisted comes back on the RunResult instead of vanishing. It does not
    make the run unshippable: the patch was verified by the final scan, and a
    logging fault does not un-verify it."""
    root = _setup_two(tmp_path)
    store = BlowUpStore("revert")

    result = run_remediation(
        FakeModel([LOGO_FIX, HERO_FIX]), store, "run-dropped-audit", root, "http://x",
        scan=_scans([VIOLATION, HERO], [HERO]),
        verifier=_boom_on_hero,
    )

    assert result.audit_complete is False
    assert result.dropped_audit == [
        {
            "step": "revert",
            "rule": "image-alt",
            "reason": "error",
            "reverted": True,
            "error": "ValueError: Failed to commit transaction in 5 attempts.",
        }
    ]
    assert "revert" not in [e["step"] for e in store.audit_trail("run-dropped-audit")]
    assert result.safe_to_ship, "a lost log line must not sink a verified run"


def test_a_revert_that_raises_is_reported_not_propagated(tmp_path, monkeypatch):
    """revert_patch touches the filesystem, so a read-only mount or a lost
    permission reaches the recovery path as OSError. That must land in
    ``unreverted`` -- the patch really is still on disk -- not abort the run."""
    root = _setup_two(tmp_path)

    def exploding_revert(patch, root_):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr("app.runner.revert_patch", exploding_revert)
    result = run_remediation(
        FakeModel([LOGO_FIX, HERO_FIX]), _store(), "run-revert-raise", root, "http://x",
        scan=_scans([VIOLATION, HERO], [HERO]),
        verifier=_boom_on_hero,
    )

    assert [p.new for p in result.verified] == ['  <img src="logo.png" alt="Logo">']
    assert result.triaged == [{"rule": "image-alt", "reason": "error", "reverted": False}]
    assert result.unreverted == [
        {
            "rule": "image-alt",
            "path": "index.html",
            "line": 3,
            "new": '  <img src="hero.png" alt="Hero">',
            "error": "OSError: [Errno 30] Read-only file system",
        }
    ]
    assert result.tree_modified is True
    assert not result.safe_to_ship
    lines = (tmp_path / "index.html").read_text().splitlines()
    assert lines[2] == '  <img src="hero.png" alt="Hero">', "still on disk, and said so"


def test_audit_failure_in_the_final_gate_does_not_abort_the_run(tmp_path):
    """_final_gate's audit write sat outside every try. A blip there discarded
    both verified patches and left them applied with no result to explain
    them -- the worst moment to fail, because every patch is already on disk."""
    root = _setup_two(tmp_path)
    store = BlowUpStore("final_scan")

    result = run_remediation(
        FakeModel([LOGO_FIX, HERO_FIX]), store, "run-gate-audit", root, "http://x",
        scan=_scans([VIOLATION, HERO], []),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )

    assert [p.new for p in result.verified] == [
        '  <img src="logo.png" alt="Logo">',
        '  <img src="hero.png" alt="Hero">',
    ]
    assert result.final_scan_ok is True
    assert result.safe_to_ship
    assert result.audit_complete is False
    assert result.dropped_audit[0]["step"] == "final_scan"


def test_an_audit_blip_mid_loop_does_not_discard_a_good_patch(tmp_path):
    """The loop's own audit writes are inside the try, so before this fix a
    failed write on the `verify` step was caught as a violation error and the
    verified patch was reverted. An RPC fault must not undo real work."""
    root = _setup(tmp_path)
    store = BlowUpStore("verify")

    result = run_remediation(
        FakeModel([LOGO_FIX]), store, "run-loop-audit", root, "http://x",
        scan=_scans([VIOLATION], []),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )

    assert [p.new for p in result.verified] == ['  <img src="logo.png" alt="Logo">']
    assert result.triaged == []
    assert result.safe_to_ship
    assert [e["step"] for e in result.dropped_audit] == ["verify"]


def test_a_gate_that_breaks_after_the_scan_says_it_could_not_verify(tmp_path, monkeypatch):
    """Last-resort guard. If grading itself breaks -- after the re-scan ran and
    with every candidate already applied -- the run still returns, reports
    ``final_scan_ok`` False instead of raising, and lists the candidates it
    left on disk: they are edits that are not in ``verified``."""
    root = _setup_two(tmp_path)

    def exploding_identity(violation):
        raise RuntimeError("grading blew up")

    monkeypatch.setattr("app.runner.identity", exploding_identity)
    result = run_remediation(
        FakeModel([LOGO_FIX, HERO_FIX]), _store(), "run-gate-broken", root, "http://x",
        scan=_scans([VIOLATION, HERO], []),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )

    assert result.verified == [], "nothing may be called verified when grading failed"
    assert result.final_scan_ok is False
    assert not result.safe_to_ship
    assert result.tree_modified is True
    assert [e["new"] for e in result.unreverted] == [
        '  <img src="logo.png" alt="Logo">',
        '  <img src="hero.png" alt="Hero">',
    ]
    assert all("final_gate_failed: RuntimeError" in e["error"] for e in result.unreverted)


# --- The gate deliberately has no under-count check ------------------------


def test_no_reappearance_implies_fewer_violations_than_the_baseline(tmp_path):
    """The under-count check the gate does not have, asserted as the property
    it already satisfies: with no over-count, the final scan cannot see more
    than ``baseline - verified`` violations. A runtime check would restate the
    counting loop rather than audit it."""
    root = _setup_two(tmp_path)
    store = _store()

    result = run_remediation(
        FakeModel([LOGO_FIX, HERO_FIX]), store, "run-undercount", root, "http://x",
        scan=_scans([VIOLATION, HERO], []),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )

    assert result.reappeared == []
    assert len(result.verified) == 2
    entry = [e for e in store.audit_trail("run-undercount") if e["step"] == "final_scan"][0]
    assert entry["baseline"] == 2
    assert entry["found"] <= entry["baseline"] - len(result.verified)


def test_a_resolution_the_run_never_targeted_is_not_a_fault(tmp_path):
    """Why an under-count must not be flagged. One patch can clear two
    violations: adding alt text to the linked image fixes ``image-alt`` and
    ``link-name`` together. ``link-name`` is still in ``expected`` -- no patch
    claimed it -- and absent from the final scan. That is the fix working, not
    a miscount."""
    (tmp_path / "index.html").write_text(f"<main>\n{MAP_LINE}\n</main>\n")
    fixed_alt = '    <a href="/map"><img src="map-thumb.png" alt="Trail map"></a>'
    store = _store()

    result = run_remediation(
        FakeModel([_reply(MAP_LINE, fixed_alt), "sorry, no idea"]),
        store, "run-twofer", str(tmp_path), "http://x",
        scan=_scans([MAP_IMAGE_ALT, MAP_LINK_NAME], []),
    )

    assert [p.new for p in result.verified] == [fixed_alt]
    assert result.triaged == [{"rule": "link-name", "reason": "no_patch"}]
    assert result.reappeared == []
    assert result.safe_to_ship
    entry = [e for e in store.audit_trail("run-twofer") if e["step"] == "final_scan"][0]
    assert (entry["baseline"], entry["found"]) == (2, 0)


def test_an_unattributed_reappearance_leaves_the_tree_matching_the_scan(tmp_path):
    """Why ``safe_to_ship``'s old wording was wrong. ``reappeared`` does not
    imply a patch was dropped: an over-count at an identity no candidate
    claimed drops nothing, so no revert runs after the final scan and the tree
    still matches what the gate saw. The run is unshippable anyway -- the page
    carries a violation it cannot account for -- but not for that reason."""
    root = _setup(tmp_path)
    stranger = Violation(
        rule="button-name",
        selector="#submit",
        html="<button></button>",
        impact="critical",
        description="Buttons must have discernible text",
    )
    result = run_remediation(
        FakeModel([LOGO_FIX]), _store(), "run-unattributed", root, "http://x",
        scan=_scans([VIOLATION], [stranger]),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )

    assert [p.new for p in result.verified] == ['  <img src="logo.png" alt="Logo">']
    assert result.reappeared == [
        {"rule": "button-name", "selector": "#submit", "seen": 1, "expected": 0}
    ]
    assert result.triaged == [], "no patch was dropped, so nothing was reverted"
    assert result.tree_modified is False
    assert not result.safe_to_ship
    lines = (tmp_path / "index.html").read_text().splitlines()
    assert lines[1] == '  <img src="logo.png" alt="Logo">', "tree is what the gate scanned"


def test_a_recovery_arm_that_breaks_still_returns_and_flags_the_tree(tmp_path, monkeypatch):
    """Last-resort guard on the loop's except arm. _audit and _revert absorb the
    store and filesystem faults underneath it, so this injects a break the arm
    does not own: whatever fails in there, the run returns, the violation is
    triaged, and the patch left applied is reported rather than assumed clean."""
    root = _setup_two(tmp_path)

    def exploding_reject(*args, **kwargs):
        raise RuntimeError("the rescue blew up")

    monkeypatch.setattr("app.runner._reject", exploding_reject)
    result = run_remediation(
        FakeModel([LOGO_FIX, HERO_FIX]), _store(), "run-recovery-broken", root, "http://x",
        scan=_scans([VIOLATION, HERO], [HERO]),
        verifier=_boom_on_hero,
    )

    assert [p.new for p in result.verified] == ['  <img src="logo.png" alt="Logo">']
    assert result.triaged == [{"rule": "image-alt", "reason": "recovery_failed"}]
    assert result.unreverted == [
        {
            "rule": "image-alt",
            "path": "index.html",
            "line": 3,
            "new": '  <img src="hero.png" alt="Hero">',
            "error": "recovery_failed: RuntimeError: the rescue blew up",
        }
    ]
    assert result.tree_modified is True
    assert not result.safe_to_ship
