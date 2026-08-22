from enum import Enum

from app.scanner import Violation, scan_page
from substrate.telemetry import log_event, span


class Verdict(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    REGRESSED = "regressed"


def identity(violation: Violation) -> tuple[str, str]:
    """How two scans decide they are looking at the same violation.

    Coarse on purpose, and the coarseness has consequences: a *second*
    violation with the same rule and selector as a baseline entry is
    indistinguishable from the baseline one, so a set comparison over these
    cannot see it. app/runner.py's final gate compares counts per identity
    to cover that; the comparison below stays set-based.
    """
    return (violation.rule, violation.selector)


def verify(url: str, target: Violation, baseline: list[Violation], scan=scan_page) -> Verdict:
    """Re-render and re-scan. Regression outranks resolution: a patch that
    fixes its target while adding a violation at an identity the baseline did
    not have is rejected.

    Two things this does not catch, both by construction:

    - a second violation at an identity the baseline already had (see
      ``identity``);
    - anything a later patch un-fixes, because ``baseline`` is frozen for the
      whole run, so every identity in it stays "known" for every later call.

    app/runner.py runs a final whole-page gate that covers both. The rule set
    is not narrowed anywhere on this path: ``scan_page`` returns everything
    axe reports, so a patch that introduces e.g. ``heading-order`` regresses
    here even though the remediation loop cannot patch that rule.
    """
    with span("a11y.verify", rule=target.rule, selector=target.selector):
        current, _ = scan(url)

    known = {identity(v) for v in baseline}
    introduced = [v for v in current if identity(v) not in known]
    if introduced:
        log_event(
            "verify.regressed",
            severity="WARNING",
            rule=target.rule,
            introduced=[v.rule for v in introduced],
        )
        return Verdict.REGRESSED

    if identity(target) in {identity(v) for v in current}:
        return Verdict.UNRESOLVED
    return Verdict.RESOLVED
