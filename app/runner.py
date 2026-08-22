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
        """True when a revert attempt returned False. The run changed a file
        and could not put it back, so the working tree carries an edit that is
        not in ``verified`` -- anything building a PR from the tree is about to
        commit it."""
        return bool(self.unreverted)

    @property
    def safe_to_ship(self) -> bool:
        """True only when the final whole-page re-scan ran, found nothing above
        the baseline, and every revert succeeded.

        ``reappeared`` covers both a target that came back and an unattributable
        new violation; either way the final snapshot no longer describes a tree
        that only contains confirmed fixes, and a drop at the gate changes the
        tree again after that snapshot was taken."""
        return self.final_scan_ok and not self.reappeared and not self.unreverted


def _revert(patch: Patch, root: str, rule: str, result: RunResult) -> bool:
    reverted = revert_patch(patch, root)
    if not reverted:
        result.unreverted.append(
            {"rule": rule, "path": patch.path, "line": patch.line, "new": patch.new}
        )
    return reverted


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
    a later patch un-did an earlier verified fix (its baseline is frozen, so a
    returning violation is still "known"), and whether two patches on the same
    source line both survived. If this scan cannot run, nothing is returned as
    verified -- an unverifiable run produces no output rather than output the
    run cannot vouch for.
    """
    try:
        final, _ = scan(url)
    except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
        log_event("run.final_scan_failed", severity="ERROR", error=repr(exc))
        store.append_audit(run_id, {"step": "final_scan", "ok": False})
        for violation, patch in fixed:
            reverted = _revert(patch, root, violation.rule, result)
            store.append_audit(
                run_id, {"step": "revert", "rule": violation.rule, "reverted": reverted}
            )
            result.triaged.append(
                {"rule": violation.rule, "reason": "final_scan_failed", "reverted": reverted}
            )
        return

    result.final_scan_ok = True
    store.append_audit(run_id, {"step": "final_scan", "ok": True, "found": len(final)})

    # Counts, not sets: a rule+selector the baseline already had can legitimately
    # appear once in the final scan and still be one *more* than this run should
    # have left behind.
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
        if identity(violation) not in over:
            result.verified.append(patch)
            continue
        reverted = _revert(patch, root, violation.rule, result)
        store.append_audit(
            run_id, {"step": "revert", "rule": violation.rule, "reverted": reverted}
        )
        result.triaged.append(
            {"rule": violation.rule, "reason": "final_scan_unresolved", "reverted": reverted}
        )
        log_event(
            "run.patch_rejected",
            severity="WARNING",
            rule=violation.rule,
            verdict="final_scan_unresolved",
            reverted=reverted,
        )


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

            applied = apply_patch(patch, root)
            store.append_audit(run_id, {"step": "apply", "rule": violation.rule, "ok": applied})
            if not applied:
                result.triaged.append({"rule": violation.rule, "reason": "apply_failed"})
                continue

            # A RESOLVED verdict here only makes the patch a *candidate*: it says
            # this patch's target was gone at this moment, against a baseline
            # frozen before the run. Nothing reaches result.verified until
            # _final_gate re-scans the finished page.
            verdict = verifier(url, violation, baseline, scan=scan)
            store.append_audit(
                run_id, {"step": "verify", "rule": violation.rule, "verdict": verdict.value}
            )

            if verdict is Verdict.RESOLVED:
                fixed.append((violation, patch))
            else:
                reverted = _revert(patch, root, violation.rule, result)
                result.triaged.append({"rule": violation.rule, "reason": verdict.value})
                log_event(
                    "run.patch_rejected",
                    severity="INFO" if reverted else "ERROR",
                    rule=violation.rule,
                    verdict=verdict.value,
                    reverted=reverted,
                )

        _final_gate(result, fixed, baseline, url, root, scan, store, run_id)

    return result
