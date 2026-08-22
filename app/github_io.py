from dataclasses import dataclass

from app.patcher import Patch
from substrate.telemetry import log_event

HANDLED_ACTIONS = {"opened", "synchronize", "reopened"}

DISCLAIMER = (
    "\n---\n"
    "Automated accessibility rules cover only a **subset of WCAG** success criteria. "
    "This pull request clears the machine-detectable floor and triages the rest to a human. "
    "It patches source and opens a pull request — it is **not an overlay**, and a human "
    "merges every change.\n\n"
    "Every fix below was verified by re-rendering the page and re-running the checker. "
    "Fixes that did not resolve their violation were reverted and listed under *Needs a human*.\n"
)


@dataclass(frozen=True)
class PullRequestRef:
    repo: str
    number: int
    head_ref: str
    head_sha: str


def parse_pr_event(payload: dict) -> PullRequestRef | None:
    if payload.get("action") not in HANDLED_ACTIONS:
        return None
    pull_request = payload.get("pull_request")
    repository = payload.get("repository")
    if not pull_request or not repository:
        return None
    return PullRequestRef(
        repo=repository["full_name"],
        number=pull_request["number"],
        head_ref=pull_request["head"]["ref"],
        head_sha=pull_request["head"]["sha"],
    )


def _body(
    patches: list[Patch],
    triaged: list[dict],
    screenshots: dict,
    dropped_audit: list[dict] | None,
) -> str:
    lines = [f"### Verified accessibility fixes ({len(patches)})", ""]
    for patch in patches:
        lines.append(f"**`{patch.path}:{patch.line}`** — {patch.rationale}")
        lines.append("")
        lines.append("```diff")
        lines.append(f"- {patch.old.strip()}")
        lines.append(f"+ {patch.new.strip()}")
        lines.append("```")
        lines.append("")
    if screenshots:
        lines.append("### Before and after")
        for label, url in screenshots.items():
            lines.append(f"- {label}: {url}")
        lines.append("")
    if triaged:
        lines.append(f"### Needs a human ({len(triaged)})")
        for item in triaged:
            lines.append(f"- `{item['rule']}` — {item['reason']}")
        lines.append("")
    # Ruling 3: the fields RunResult grew beyond the original brief must be
    # surfaced somewhere a human sees them. `dropped_audit` is the one that
    # can be true on an otherwise-safe run (RunResult.safe_to_ship does not
    # depend on it — see app/runner.py) so it is the one this body still
    # needs to speak for; every other unsafe signal (tree_modified,
    # reappeared, an unreverted patch) already keeps the worker from calling
    # this function at all (see job/worker.py's safe_to_ship gate).
    if dropped_audit:
        lines.append("### Audit trail")
        lines.append(
            f"This run's audit trail is **incomplete**: {len(dropped_audit)} entr"
            f"{'y' if len(dropped_audit) == 1 else 'ies'} could not be written to "
            "the store. The fixes above were still verified in-process — this "
            "notes a gap in the *record* of the run, not in the verification "
            "itself."
        )
        lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def open_fix_pr(
    client,
    ref: PullRequestRef,
    patches: list[Patch],
    triaged: list[dict],
    screenshots: dict,
    dropped_audit: list[dict] | None = None,
) -> str | None:
    if not patches:
        log_event("pr.skipped", reason="no_verified_patches", repo=ref.repo, pr=ref.number)
        return None
    repo = client.get_repo(ref.repo)
    pull_request = repo.create_pull(
        title=f"Fix {len(patches)} verified accessibility violation(s)",
        body=_body(patches, triaged, screenshots, dropped_audit),
        head=f"a11y-fixes/{ref.head_sha[:7]}",
        base=ref.head_ref,
    )
    log_event("pr.opened", repo=ref.repo, url=pull_request.html_url, fixes=len(patches))
    return pull_request.html_url
