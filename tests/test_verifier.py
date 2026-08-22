from app.scanner import Violation
from app.verifier import Verdict, verify

TARGET = Violation(
    rule="image-alt", selector=".logo", html="<img>", impact="critical", description="d"
)
OTHER = Violation(
    rule="label", selector="#notify", html="<input>", impact="serious", description="d"
)
NEW_ONE = Violation(
    rule="color-contrast", selector=".x", html="<p>", impact="serious", description="d"
)


def _scanner(returns: list[Violation]):
    return lambda url: (returns, b"png")


def test_resolved_when_target_is_gone():
    assert verify("http://x", TARGET, baseline=[TARGET, OTHER], scan=_scanner([OTHER])) is Verdict.RESOLVED


def test_unresolved_when_target_remains():
    assert verify("http://x", TARGET, baseline=[TARGET], scan=_scanner([TARGET])) is Verdict.UNRESOLVED


def test_regressed_when_a_new_violation_appears():
    verdict = verify("http://x", TARGET, baseline=[TARGET], scan=_scanner([NEW_ONE]))
    assert verdict is Verdict.REGRESSED


def test_regression_outranks_resolution():
    """Fixing the target while breaking something else is still a regression."""
    verdict = verify("http://x", TARGET, baseline=[TARGET, OTHER], scan=_scanner([OTHER, NEW_ONE]))
    assert verdict is Verdict.REGRESSED


def test_unchanged_unrelated_violations_do_not_count_as_regression():
    verdict = verify("http://x", TARGET, baseline=[TARGET, OTHER], scan=_scanner([OTHER]))
    assert verdict is Verdict.RESOLVED
