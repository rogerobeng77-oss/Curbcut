from enum import Enum

from app.scanner import Violation, scan_page
from substrate.telemetry import log_event, span


class Verdict(str, Enum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    REGRESSED = "regressed"


def _identity(violation: Violation) -> tuple[str, str]:
    return (violation.rule, violation.selector)


def verify(url: str, target: Violation, baseline: list[Violation], scan=scan_page) -> Verdict:
    """Re-render and re-scan. Regression outranks resolution: a patch that fixes
    its target while introducing anything new is rejected."""
    with span("a11y.verify", rule=target.rule, selector=target.selector):
        current, _ = scan(url)

    known = {_identity(v) for v in baseline}
    introduced = [v for v in current if _identity(v) not in known]
    if introduced:
        log_event(
            "verify.regressed",
            severity="WARNING",
            rule=target.rule,
            introduced=[v.rule for v in introduced],
        )
        return Verdict.REGRESSED

    if _identity(target) in {_identity(v) for v in current}:
        return Verdict.UNRESOLVED
    return Verdict.RESOLVED
