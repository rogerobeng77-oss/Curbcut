import json
from dataclasses import dataclass

from app.locator import SourceMatch
from app.prompts import PATCH_INSTRUCTION
from app.scanner import Violation
from substrate.telemetry import log_event, span


# Everything str.splitlines() treats as a line break. app/applier.py splits the
# file with splitlines() and rejoins with "\n", so a replacement containing any
# of these grows the file by a line: apply succeeds, and revert then finds
# lines[index] != patch.new and cannot restore the file. A rejected patch would
# stay on disk and get swept into the PR by `git commit -am`. One line in, one
# line out keeps apply and revert symmetrical. It is not a guarantee of
# reversibility on its own: a later patch that rewrites the same line still
# defeats revert, which is why app/runner.py reports every revert outcome.
LINE_BREAKS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"


def contains_line_break(text: str) -> bool:
    return any(char in text for char in LINE_BREAKS)


class MalformedPatchError(ValueError):
    """The model returned something that is not a usable patch."""


@dataclass(frozen=True)
class Patch:
    path: str
    line: int
    old: str
    new: str
    rationale: str
    # Defaulted, not threaded through every existing call site: the axe rule
    # this patch targets, carried only so the console can label a verified
    # fix the same way it labels a triaged one (see job/worker.py's
    # build_run_record). propose_patch is the one caller that knows the
    # violation, so it is the one caller that sets this.
    rule: str = ""


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        return body.rsplit("```", 1)[0].strip()
    return stripped


# Vertex JSON mode constrains decoding to this shape, so the reply cannot
# arrive wrapped in prose or a code fence. _strip_fences stays anyway: a model
# without JSON-mode support (and FakeModel, which does not honour the schema)
# still reaches json.loads through this path.
PATCH_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "old": {"type": "STRING"},
        "new": {"type": "STRING"},
        "rationale": {"type": "STRING"},
    },
    "required": ["old", "new", "rationale"],
}


def propose_patch(model, violation: Violation, match: SourceMatch, screenshot: bytes) -> Patch | None:
    prompt = PATCH_INSTRUCTION.format(
        rule=violation.rule,
        impact=violation.impact,
        description=violation.description,
        selector=violation.selector,
        html=violation.html,
        path=match.path,
        line=match.line,
        source_line=match.text,
    )
    with span("a11y.propose_patch", rule=violation.rule, path=match.path):
        reply = model.generate(prompt, images=[screenshot], response_schema=PATCH_SCHEMA)

    try:
        payload = json.loads(_strip_fences(reply))
    except json.JSONDecodeError:
        # Log what actually came back. The first time this fired in production
        # the reply was discarded, which left nothing to diagnose from -- only
        # the knowledge that three patches vanished.
        log_event(
            "patch.malformed",
            severity="WARNING",
            rule=violation.rule,
            reason="not_json",
            reply_head=reply[:200],
        )
        return None

    if not all(key in payload for key in ("old", "new", "rationale")):
        log_event("patch.malformed", severity="WARNING", rule=violation.rule, reason="missing_keys")
        return None
    if payload["old"] != match.text:
        log_event("patch.malformed", severity="WARNING", rule=violation.rule, reason="old_mismatch")
        return None
    if payload["new"] == payload["old"]:
        log_event("patch.malformed", severity="WARNING", rule=violation.rule, reason="noop")
        return None
    if contains_line_break(payload["new"]):
        log_event("patch.malformed", severity="WARNING", rule=violation.rule, reason="multiline")
        return None

    return Patch(
        path=match.path,
        line=match.line,
        old=payload["old"],
        new=payload["new"],
        rationale=payload["rationale"],
        rule=violation.rule,
    )
