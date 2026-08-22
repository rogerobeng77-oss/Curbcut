from dataclasses import dataclass, field

from app.applier import apply_patch, revert_patch
from app.locator import locate
from app.patcher import Patch, propose_patch
from app.scanner import scan_page
from app.verifier import Verdict, verify
from substrate.store import Store
from substrate.telemetry import log_event, span


@dataclass
class RunResult:
    verified: list[Patch] = field(default_factory=list)
    triaged: list[dict] = field(default_factory=list)


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

        for violation in baseline:
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

            # INVARIANT: verify() is called for every applied patch, and only a
            # RESOLVED verdict may add the patch to result.verified. Every other
            # branch reverts the file change before recording the triage reason,
            # so nothing that reaches the caller through `verified` bypassed a
            # fresh re-scan that confirmed the target violation is actually gone
            # and nothing new appeared.
            verdict = verifier(url, violation, baseline, scan=scan)
            store.append_audit(
                run_id, {"step": "verify", "rule": violation.rule, "verdict": verdict.value}
            )

            if verdict is Verdict.RESOLVED:
                result.verified.append(patch)
            else:
                reverted = revert_patch(patch, root)
                result.triaged.append({"rule": violation.rule, "reason": verdict.value})
                log_event(
                    "run.patch_rejected",
                    severity="INFO" if reverted else "ERROR",
                    rule=violation.rule,
                    verdict=verdict.value,
                    reverted=reverted,
                )

    return result
