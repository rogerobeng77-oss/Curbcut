import json
from dataclasses import dataclass

from app.locator import SourceMatch
from app.prompts import PATCH_INSTRUCTION
from app.scanner import Violation
from substrate.telemetry import log_event, span


class MalformedPatchError(ValueError):
    """The model returned something that is not a usable patch."""


@dataclass(frozen=True)
class Patch:
    path: str
    line: int
    old: str
    new: str
    rationale: str


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        return body.rsplit("```", 1)[0].strip()
    return stripped


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
        reply = model.generate(prompt, images=[screenshot])

    try:
        payload = json.loads(_strip_fences(reply))
    except json.JSONDecodeError:
        log_event("patch.malformed", severity="WARNING", rule=violation.rule, reason="not_json")
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

    return Patch(
        path=match.path,
        line=match.line,
        old=payload["old"],
        new=payload["new"],
        rationale=payload["rationale"],
    )
