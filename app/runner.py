from collections import Counter
from dataclasses import dataclass, field

from app.applier import apply_patch, revert_patch
from app.locator import locate
from app.patcher import Patch, propose_patch
from app.scanner import SUPPORTED_RULES, Violation, scan_page
from app.verifier import Verdict, identity, verify
from substrate.store import Store
from substrate.telemetry import log_event, span


@dataclass
class RunResult:
    verified: list[Patch] = field(default_factory=list)
    triaged: list[dict] = field(default_factory=list)
    final_scan_ok: bool = False
    reappeared: list[dict] = field(default_factory=list)
    unreverted: list[dict] = field(default_factory=list)

    @property
    def tree_modified(self) -> bool:
        """True when a revert attempt returned False: this run changed a file
        and could not put it back, so the working tree carries an edit that is
        not in ``verified``. A caller running `git commit -am` would ship it."""
        return bool(self.unreverted)

    @property
    def safe_to_ship(self) -> bool:
        """True only when the final whole-page re-scan ran, found nothing above
        the baseline, and every revert this run attempted succeeded.

        ``reappeared`` covers both a target that came back and a new violation
        nobody attributed to a patch; either way the page is worse than the
        gate's snapshot describes, because dropping a patch at the gate edits
        the tree again after that snapshot was taken.
        """
        return self.final_scan_ok and not self.reappeared and not self.unreverted


def _revert(patch: Patch, root: str, rule: str, result: RunResult) -> bool:
    reverted = revert_patch(patch, root)
    if not reverted:
        result.unreverted.append(
            {"rule": rule, "path": patch.path, "line": patch.line, "new": patch.new}
        )
    return reverted


def _reject(
    patch: Patch,
    violation: Violation,
    reason: str,
    result: RunResult,
    root: str,
    store: Store,
    run_id: str,
    severity: str = "INFO",
) -> None:
    """Undo a patch that will not be returned, and say out loud whether the undo
    worked. ``reverted=False`` means the file is still modified: the triage
    entry, the audit trail and ``RunResult.tree_modified`` all carry that,
    because a caller building a PR from the working tree cannot see it
    otherwise."""
    reverted = _revert(patch, root, violation.rule, result)
    store.append_audit(
        run_id, {"step": "revert", "rule": violation.rule, "reason": reason, "reverted": reverted}
    )
    result.triaged.append({"rule": violation.rule, "reason": reason, "reverted": reverted})
    log_event(
        "run.patch_rejected",
        severity=severity if reverted else "ERROR",
        rule=violation.rule,
        verdict=reason,
        reverted=reverted,
    )


def _final_gate(
    result: RunResult,
    fixed: list[tuple[Violation, Patch]],
    baseline: list[Violation],
    url: str,
    root: str,
    scan,
    store: Store,
    run_id: str,
) -> None:
    """One whole-page re-scan after the loop, and the only thing that may put a
    patch into ``result.verified``.

    Per-violation ``verify`` cannot answer the two questions this does: whether
    a later patch un-did an earlier verified fix (``verify``'s baseline is
    frozen, so a violation that comes back is still "known" and never counts as
    introduced), and whether two patches that rewrote the same source line both
    survived. If this scan cannot run, nothing is returned as verified: an
    unverifiable run produces no output rather than output it cannot vouch for.
    """
    try:
        final, _ = scan(url)
    except Exception as exc:  # noqa: BLE001 - any scan failure means no verdict
        log_event("run.final_scan_failed", severity="ERROR", error=f"{type(exc).__name__}: {exc}")
        store.append_audit(run_id, {"step": "final_scan", "ok": False})
        for violation, patch in fixed:
            _reject(
                patch, violation, "final_scan_failed", result, root, store, run_id, "ERROR"
            )
        return

    result.final_scan_ok = True
    store.append_audit(run_id, {"step": "final_scan", "ok": True, "found": len(final)})

    # Counts, not sets: a rule+selector pair the baseline already carried can
    # appear once in the final scan and still be one *more* than this run should
    # have left behind -- either the fixed one came back, or a second node now
    # has the same identity. A set comparison sees neither.
    expected = Counter(identity(v) for v in baseline)
    for violation, _patch in fixed:
        expected[identity(violation)] -= 1
    seen = Counter(identity(v) for v in final)
    over = {ident for ident, count in seen.items() if count > expected[ident]}

    for rule, selector in sorted(over):
        result.reappeared.append(
            {
                "rule": rule,
                "selector": selector,
                "seen": seen[(rule, selector)],
                "expected": max(expected[(rule, selector)], 0),
            }
        )
        log_event("run.final_scan_regression", severity="WARNING", rule=rule, selector=selector)

    for violation, patch in fixed:
        if identity(violation) in over:
            _reject(
                patch, violation, "final_scan_unresolved", result, root, store, run_id, "WARNING"
            )
        else:
            result.verified.append(patch)


def run_remediation(
    model,
    store: Store,
    run_id: str,
    root: str,
    url: str,
    scan=scan_page,
    verifier=verify,
) -> RunResult:
    result = RunResult()

    with span("a11y.run", run_id=run_id):
        baseline, screenshot = scan(url)
        store.append_audit(run_id, {"step": "scan", "found": len(baseline)})
        fixed: list[tuple[Violation, Patch]] = []

        for violation in baseline:
            if violation.rule not in SUPPORTED_RULES:
                store.append_audit(run_id, {"step": "skip", "rule": violation.rule})
                result.triaged.append({"rule": violation.rule, "reason": "unsupported_rule"})
                continue

            # One violation blowing up must not cost the run every patch already
            # verified, and must never leave its own patch on disk. A raising
            # verifier (Playwright timeout) and a raising model (vertex 429
            # RESOURCE_EXHAUSTED) are the two likeliest live failures; both land
            # in the handler below.
            applied: Patch | None = None
            try:
                match = locate(violation, root)
                store.append_audit(
                    run_id,
                    {"step": "locate", "rule": violation.rule, "found": match is not None},
                )
                if match is None:
                    result.triaged.append({"rule": violation.rule, "reason": "not_located"})
                    continue

                patch = propose_patch(model, violation, match, screenshot)
                store.append_audit(
                    run_id,
                    {"step": "propose", "rule": violation.rule, "proposed": patch is not None},
                )
                if patch is None:
                    result.triaged.append({"rule": violation.rule, "reason": "no_patch"})
                    continue

                ok = apply_patch(patch, root)
                store.append_audit(run_id, {"step": "apply", "rule": violation.rule, "ok": ok})
                if not ok:
                    result.triaged.append({"rule": violation.rule, "reason": "apply_failed"})
                    continue
                applied = patch

                # A RESOLVED verdict here only makes the patch a *candidate*: it
                # says this patch's target was gone at this moment, measured
                # against a baseline frozen before the run started. Nothing
                # reaches result.verified until _final_gate re-scans the
                # finished page.
                verdict = verifier(url, violation, baseline, scan=scan)
                store.append_audit(
                    run_id, {"step": "verify", "rule": violation.rule, "verdict": verdict.value}
                )

                if verdict is Verdict.RESOLVED:
                    fixed.append((violation, patch))
                    applied = None  # left on disk on purpose, pending the gate
                    continue

                _reject(patch, violation, verdict.value, result, root, store, run_id)
            except Exception as exc:  # noqa: BLE001 - see the comment above
                store.append_audit(
                    run_id,
                    {"step": "error", "rule": violation.rule, "error": type(exc).__name__},
                )
                log_event(
                    "run.violation_failed",
                    severity="ERROR",
                    rule=violation.rule,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if applied is None:
                    result.triaged.append({"rule": violation.rule, "reason": "error"})
                else:
                    _reject(applied, violation, "error", result, root, store, run_id, "ERROR")

        _final_gate(result, fixed, baseline, url, root, scan, store, run_id)

    return result
