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
    dropped_audit: list[dict] = field(default_factory=list)

    @property
    def tree_modified(self) -> bool:
        """True when this run edited a file and cannot say it put the file back:
        a revert returned False, a revert raised, or a recovery path failed
        before it could establish the file's state. The working tree then
        carries an edit that is not in ``verified``, and a caller running
        `git commit -am` would ship it.

        Deliberately one-sided. The entries written by a failed recovery say
        the state is *unknown*, not that the file is definitely modified, and
        they still set this True. Calling a clean tree dirty costs one run;
        calling a dirty tree clean ships an unverified edit."""
        return bool(self.unreverted)

    @property
    def audit_complete(self) -> bool:
        """True when every audit entry this run produced reached the store.

        False means the trail in Firestore has a hole and ``dropped_audit``
        holds exactly what is missing from it, so the demo view can render the
        gap instead of showing a shorter trail that looks well-formed."""
        return not self.dropped_audit

    @property
    def safe_to_ship(self) -> bool:
        """True only when the final whole-page re-scan ran, saw nothing beyond
        what the run should have left behind, and had no revert fail.

        When it is True the final scan observed the exact tree that would be
        committed: every rejected patch was reverted before that scan, and any
        patch dropped *at* the gate puts an entry in ``reappeared``.

        ``reappeared`` covers two different things, and neither is shippable. A
        target that came back is dropped at the gate, and dropping it reverts
        the file *after* the scan, so the committed tree is no longer the tree
        the gate looked at. A violation at an identity no patch in the run
        claimed is left alone -- the tree does match the scan there -- but the
        page carries a violation this run cannot account for.

        Not part of this: ``audit_complete``. A patch is verified by the final
        scan, which happens in this process; whether the record of that scan
        reached Firestore does not change what the scan saw. A dropped audit
        entry is reported (``dropped_audit``) and never silently swallowed, but
        it must not be able to sink a run whose patches are good.
        """
        return self.final_scan_ok and not self.reappeared and not self.unreverted


def _audit(store: Store, run_id: str, entry: dict, result: RunResult) -> None:
    """Append one audit entry, without letting a store failure escape.

    ``Store.append_audit`` is a Firestore transaction that raises on its own
    retry ceiling (see substrate/store.py), and it is called from recovery
    paths -- the loop's ``except`` arm and the final gate. An RPC that raises
    there turns a handled failure into an unhandled one and costs the caller
    every patch the run had already verified, for a *logging* fault.

    The entry is not dropped on the floor either: the trail is the reasoning
    chain the demo renders, so what could not be persisted is kept on the
    RunResult and the hole is announced at ERROR. Callers get the whole trail
    by reading ``store.audit_trail(run_id)`` and ``result.dropped_audit``
    together; ``seq`` is stamped store-side, so a dropped entry leaves no gap
    in the numbering to notice, which is why it has to be carried out of band.
    """
    try:
        store.append_audit(run_id, entry)
    except Exception as exc:  # noqa: BLE001 - an audit write must not kill a run
        # Recorded before it is logged: the RunResult is the copy the caller
        # acts on, and log_event writes to stdout, which is not this process's
        # to guarantee.
        result.dropped_audit.append(dict(entry, error=f"{type(exc).__name__}: {exc}"))
        log_event(
            "run.audit_write_failed",
            severity="ERROR",
            step=str(entry.get("step")),
            error=f"{type(exc).__name__}: {exc}",
        )


def _revert(patch: Patch, root: str, rule: str, result: RunResult) -> bool:
    """Undo one patch. A revert that raises counts exactly like a revert that
    returned False: the file may still be modified and the caller is told so.
    ``revert_patch`` reads and writes a file, so a permission change, a
    read-only mount or a vanished directory all reach here as OSError."""
    error = None
    try:
        reverted = revert_patch(patch, root)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        reverted = False
        error = f"{type(exc).__name__}: {exc}"
        log_event(
            "run.revert_raised", severity="ERROR", rule=rule, path=patch.path, error=error
        )
    if not reverted:
        entry = {"rule": rule, "path": patch.path, "line": patch.line, "new": patch.new}
        if error is not None:
            entry["error"] = error
        result.unreverted.append(entry)
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
    otherwise.

    Every step here is failure-tolerant on purpose -- this function *is* the
    recovery path for the loop and for the gate, and a recovery path that
    raises destroys the run it was supposed to rescue."""
    reverted = _revert(patch, root, violation.rule, result)
    _audit(
        store,
        run_id,
        {"step": "revert", "rule": violation.rule, "reason": reason, "reverted": reverted},
        result,
    )
    result.triaged.append({"rule": violation.rule, "reason": reason, "reverted": reverted})
    log_event(
        "run.patch_rejected",
        severity=severity if reverted else "ERROR",
        rule=violation.rule,
        verdict=reason,
        reverted=reverted,
    )


def _recover(
    exc: Exception,
    violation: Violation,
    applied: Patch | None,
    result: RunResult,
    root: str,
    store: Store,
    run_id: str,
) -> None:
    """The loop's recovery arm for one violation that blew up.

    Wrapped in its own guard because the arm has two moving parts that can
    themselves fail -- the audit RPC and the revert -- and this is where the
    run is being rescued, so nothing here may propagate. ``_audit`` and
    ``_revert`` already absorb their own faults; the outer ``except`` is the
    last resort for anything neither of them owns, and it records the patch as
    a tree edit of unknown state rather than pretending the file is clean."""
    try:
        _audit(
            store,
            run_id,
            {"step": "error", "rule": violation.rule, "error": type(exc).__name__},
            result,
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
    except Exception as inner:  # noqa: BLE001 - the rescue itself failed
        result.triaged.append({"rule": violation.rule, "reason": "recovery_failed"})
        if applied is not None:
            result.unreverted.append(
                {
                    "rule": violation.rule,
                    "path": applied.path,
                    "line": applied.line,
                    "new": applied.new,
                    "error": f"recovery_failed: {type(inner).__name__}: {inner}",
                }
            )
        log_event(
            "run.recovery_failed",
            severity="ERROR",
            rule=violation.rule,
            error=f"{type(inner).__name__}: {inner}",
        )


def _grade(
    result: RunResult,
    fixed: list[tuple[Violation, Patch]],
    final: list[Violation],
    baseline: list[Violation],
    root: str,
    store: Store,
    run_id: str,
) -> None:
    """Compare the final scan against what the run should have left behind, and
    decide which candidates become ``result.verified``.

    Counts, not sets: a rule+selector pair the baseline already carried can
    appear once in the final scan and still be one *more* than this run should
    have left behind -- either the fixed one came back, or a second node now
    has the same identity. A set comparison sees neither.

    Over-counts only. There is deliberately no check that the final scan saw
    *fewer* violations than the baseline, because with no over-count that is
    already true and cannot be otherwise: ``expected`` is the baseline counter
    minus one per candidate, every candidate came from the baseline so no
    identity goes negative, and ``sum(expected) == len(baseline) - len(fixed)``.
    An empty ``over`` means ``seen[i] <= expected[i]`` for every identity, so
    ``len(final) <= len(baseline) - len(verified)``. A separate assertion of
    that is a restatement of this loop, not a second opinion on it.
    An *under*-count is not a fault at all and must not be flagged: one patch
    can resolve two violations (adding alt text to a linked image clears both
    ``image-alt`` and ``link-name``), so a violation the run never targeted can
    legitimately be gone from the final scan.
    """
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

    Nothing in here propagates. A gate that raises is the worst version of this
    failure -- it happens after every patch has been applied, so it strands all
    of them on disk and returns no result at all.
    """
    try:
        final, _ = scan(url)
    except Exception as exc:  # noqa: BLE001 - any scan failure means no verdict
        log_event("run.final_scan_failed", severity="ERROR", error=f"{type(exc).__name__}: {exc}")
        _audit(store, run_id, {"step": "final_scan", "ok": False}, result)
        for violation, patch in fixed:
            _reject(
                patch, violation, "final_scan_failed", result, root, store, run_id, "ERROR"
            )
        return

    result.final_scan_ok = True
    _audit(
        store,
        run_id,
        {"step": "final_scan", "ok": True, "found": len(final), "baseline": len(baseline)},
        result,
    )

    try:
        _grade(result, fixed, final, baseline, root, store, run_id)
    except Exception as exc:  # noqa: BLE001 - last resort; see below
        # Reached only if grading itself breaks: _audit and _revert absorb the
        # store and filesystem faults underneath it, so this is a guard against
        # what is not foreseen rather than a known failure. The scan ran, but
        # its verdict is incomplete, so the run stops claiming it was verified.
        # Candidates the loop never reached are still applied on disk and are
        # not in `verified`, which is exactly what `unreverted` means.
        result.final_scan_ok = False
        log_event("run.final_gate_failed", severity="ERROR", error=f"{type(exc).__name__}: {exc}")
        _audit(store, run_id, {"step": "final_gate", "ok": False}, result)
        graded = {id(patch) for patch in result.verified}
        for violation, patch in fixed:
            if id(patch) not in graded:
                result.unreverted.append(
                    {
                        "rule": violation.rule,
                        "path": patch.path,
                        "line": patch.line,
                        "new": patch.new,
                        "error": f"final_gate_failed: {type(exc).__name__}: {exc}",
                    }
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
    """Always returns a RunResult once the baseline scan is in, whatever the
    loop or the gate hit on the way.

    The baseline scan itself is the one call left to propagate: it runs before
    anything is applied, so a failure there loses no work and produces no
    result worth returning -- the caller wants the traceback, not an empty
    RunResult that reads like a clean page.
    """
    result = RunResult()

    with span("a11y.run", run_id=run_id):
        baseline, screenshot = scan(url)
        _audit(store, run_id, {"step": "scan", "found": len(baseline)}, result)
        fixed: list[tuple[Violation, Patch]] = []

        for violation in baseline:
            if violation.rule not in SUPPORTED_RULES:
                _audit(store, run_id, {"step": "skip", "rule": violation.rule}, result)
                result.triaged.append({"rule": violation.rule, "reason": "unsupported_rule"})
                continue

            # One violation blowing up must not cost the run every patch already
            # verified, and must not leave its own patch on disk without saying
            # so -- the revert can itself fail, and then it is reported. A raising
            # verifier (Playwright timeout) and a raising model (vertex 429
            # RESOURCE_EXHAUSTED) are the two likeliest live failures; both land
            # in the handler below.
            applied: Patch | None = None
            try:
                match = locate(violation, root)
                _audit(
                    store,
                    run_id,
                    {"step": "locate", "rule": violation.rule, "found": match is not None},
                    result,
                )
                if match is None:
                    result.triaged.append({"rule": violation.rule, "reason": "not_located"})
                    continue

                patch = propose_patch(model, violation, match, screenshot)
                _audit(
                    store,
                    run_id,
                    {"step": "propose", "rule": violation.rule, "proposed": patch is not None},
                    result,
                )
                if patch is None:
                    result.triaged.append({"rule": violation.rule, "reason": "no_patch"})
                    continue

                ok = apply_patch(patch, root)
                _audit(store, run_id, {"step": "apply", "rule": violation.rule, "ok": ok}, result)
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
                _audit(
                    store,
                    run_id,
                    {"step": "verify", "rule": violation.rule, "verdict": verdict.value},
                    result,
                )

                if verdict is Verdict.RESOLVED:
                    fixed.append((violation, patch))
                    applied = None  # left on disk on purpose, pending the gate
                    continue

                _reject(patch, violation, verdict.value, result, root, store, run_id)
            except Exception as exc:  # noqa: BLE001 - see the comment above
                _recover(exc, violation, applied, result, root, store, run_id)

        _final_gate(result, fixed, baseline, url, root, scan, store, run_id)

    return result
