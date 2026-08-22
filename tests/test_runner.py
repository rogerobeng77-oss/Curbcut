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


def _setup(tmp_path):
    (tmp_path / "index.html").write_text('<div>\n  <img src="logo.png">\n</div>\n')
    return str(tmp_path)


def test_verified_patch_is_kept(tmp_path):
    root = _setup(tmp_path)
    model = FakeModel([_reply('  <img src="logo.png">', '  <img src="logo.png" alt="Logo">')])
    result = run_remediation(
        model, _store(), "run-1", root, "http://x",
        scan=lambda url: ([VIOLATION], b"png"),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )
    assert len(result.verified) == 1
    assert result.triaged == []


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
        scan=lambda url: ([VIOLATION], b"png"),
        verifier=lambda url, target, baseline, scan=None: Verdict.RESOLVED,
    )
    steps = [entry["step"] for entry in store.audit_trail("run-5")]
    assert steps == ["scan", "locate", "propose", "apply", "verify"]


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
