import re
from dataclasses import dataclass

# Both repeats around the "@" are bounded, and that is load-bearing rather than
# cosmetic: with `+` on either side this pattern is quadratic in the length of
# the text, and redact_pii is reachable from the wire (substrate.web logs a
# redacted exception detail for every failed event) and from every audit write
# (substrate.store._redact). Measured on the unbounded form, doubling the input
# quadrupled the time -- 2 KB 0.004 s, 4 KB 0.015 s, 8 KB 0.056 s, 16 KB
# 0.230 s, 32 KB 0.908 s, 64 KB 3.611 s -- which is minutes at Pub/Sub's 10 MB
# message cap, on the event loop, inside the arm that exists to keep the
# endpoint returning 204.
#
# There were two independent quadratic sources, not one, and bounding only the
# domain leaves the other live:
#
#   - the domain, `[\w.-]+\.`: `[\w.-]` contains the dot, so the engine can
#     split "a.a.a.a…" between the run and the literal in a linear number of
#     ways, and retries each one. Worst case `a@a.a.a.a…`.
#   - the local part, `[\w.%+-]+@`: this one is not catastrophic backtracking
#     but plain O(n) work at each of O(n) start positions. `\b` offers a start
#     position at every label, and from each one the run scans forward to the
#     "@" before failing. Worst case `a.a.a.a…@`.
#
# The bounds are RFC 5321's own limits -- 64 octets of local part, 255 of
# domain -- so no address that could be delivered is affected. They make the
# work at each start position a constant rather than O(n): measured after this
# change, all three adversarial shapes double rather than quadruple (see
# tests/test_guards.py::test_redaction_is_linear_in_input_length).
#
# The local-part bound is what keeps the *unbounded* TLD repeat safe as well:
# `[A-Za-z]{2,}` backtracks through a long letter run when the character after
# it is a word character that is not a letter, but only a start position within
# 64 characters of an "@" can reach that repeat at all, so the fan-in is capped
# and the total stays linear. The TLD is therefore left unbounded, which keeps
# arbitrary-length TLDs redacting as they did before.
#
# What the bounds cost: an over-limit address does not simply fail to redact
# as a whole. Measured across five over-limit shapes, three of five redact
# only in part and leave a fragment of the address in the output:
#
#   - domain > 255 chars but with a "." within reach of the cap: the domain
#     quantifier backtracks to the last "." + TLD that fits within 255 chars,
#     so the match still fires, just starting later or ending earlier than
#     the full address, and a trailing remainder survives literally. E.g.
#     "sam@" + "sub."*63 + "example.com" (263-char domain) redacts to
#     "[EMAIL].com", and "sam@" + "sub."*250 + "example.com" (1000-char
#     domain) redacts to "[EMAIL]" followed by ~750 surviving characters of
#     "sub.sub...." tail.
#   - local part > 64 chars but containing a "." within reach: the local
#     quantifier's \b start positions let an in-bounds slice starting later
#     in the local part match instead, so the leading local-part characters
#     survive literally as a prefix. E.g. "verylongname."*7 + "sam@example.com"
#     (91-char local part) redacts to
#     "verylongname.verylongname.verylongname[EMAIL]".
#
# Total escape -- nothing redacted at all -- happens only in the two shapes
# with no such "." to backtrack onto:
#
#   - local part > 64 chars with no separator anywhere in it, e.g. 65 bare
#     word characters before "@": every \b start position is still more than
#     64 characters from "@", so no start position can reach it.
#   - domain > 255 chars with no "." in the first 255 characters after "@",
#     e.g. "sam@" + "d"*300 + ".com": the domain quantifier can never reach
#     an anchoring "." within its cap, so the whole pattern fails to match --
#     and because nothing else in redact_pii claims the span, the local part
#     leaks too.
#
# Both are longer than any deliverable address. That is the deliberate trade
# -- a pathological string is no longer a denial of service -- and the
# truncation in substrate.web bounds the wire path a second time (see there
# for the size of the fragment truncation itself can leave unredacted).
_EMAIL = re.compile(r"\b[\w.%+-]{1,64}@[\w.-]{1,255}\.[A-Za-z]{2,}\b")

# Both number patterns accept hyphen, dot, space or no separator at all, and
# are bounded by "not a digit" on either side rather than \b: a word boundary
# would miss a run glued to a letter (``tel4155550132``), and a leak is worse
# than over-redacting an ambiguous digit run. The bounds also keep the two
# patterns disjoint by digit count -- SSN is exactly 9, phone is 10 (plus an
# optional leading US country code) -- so neither can claim the other's span.
_SSN = re.compile(r"(?<!\d)\d{3}[-. ]?\d{2}[-. ]?\d{4}(?!\d)")
_PHONE = re.compile(
    r"(?<!\d)(?:\+?1[-. ]?)?(?:\(\d{3}\)[-. ]?|\d{3}[-. ]?)\d{3}[-. ]?\d{4}(?!\d)"
)

_OVERRIDE_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"disregard\s+(all\s+)?(prior|previous)\s+", re.I),
    re.compile(r"reveal\s+your\s+(system\s+)?prompt", re.I),
]


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str | None
    text: str


def redact_pii(text: str) -> str:
    # Email is matched first because the number patterns would otherwise
    # consume an email's local part and leave the domain behind:
    # "415-555-0132@example.com" would persist as "[PHONE]@example.com".
    # SSN then runs before phone. Neither can claim the other's span (see the
    # pattern comment above), but in a run of several adjacent digit groups
    # they partition it differently depending on which matches first:
    # "546 742173 6614" redacts to "[SSN] 6614" in this order, and to
    # "546 [PHONE]" if the two .sub() calls are swapped. SSN goes first so the
    # more sensitive reading wins.
    text = _EMAIL.sub("[EMAIL]", text)
    text = _SSN.sub("[SSN]", text)
    text = _PHONE.sub("[PHONE]", text)
    return text


def check_input(text: str) -> GuardResult:
    for pattern in _OVERRIDE_PATTERNS:
        if pattern.search(text):
            return GuardResult(allowed=False, reason="instruction_override", text=redact_pii(text))
    return GuardResult(allowed=True, reason=None, text=redact_pii(text))
