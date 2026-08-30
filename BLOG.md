# Building an accessibility agent that proves its own fixes

*Written for the purposes of entering the All Things Agentic Hackathon (Taskmaster track).*

## The problem

The WebAIM Million is an annual scan of the top one million home pages on the
web, checked with automated accessibility tooling. The February 2026 edition
found that **95.9%** of them have at least one detectable WCAG failure — up
from 94.8% the year before. Six years of slow improvement just reversed.

That number is uncomfortable for a specific reason: none of it requires human
judgment to find. Missing alt text, unlabeled form fields, unnamed buttons,
insufficient color contrast — these are things a scanner has been able to
catch for free, for years. Detection was never the bottleneck. Nobody had
time to act on the report.

It is also worth being concrete about who this costs. A button with no
accessible name is announced by a screen reader as one word: "button". Not
"search", not "pay". You are asked to click something with no idea what it
does. Names and labels matter to people using screen readers; contrast
matters to anyone with low vision, ageing eyes, or a screen in sunlight,
which is a far larger population than most teams assume. In everything we
scanned, contrast was the single most common failure — 1,525 of 4,113.

## We did not want to build on someone else's statistic

The WebAIM figure covers home pages of the top million sites. We wanted to
know whether it held on a population nobody surveys, so we pointed this
project's own scanner at 199 real project and documentation sites published
from GitHub. 130 responded. **127 of them had violations — 97.7%.**

| | |
|---|---|
| Reachable sites with at least one violation | **127 of 130 (97.7%)** |
| Total node-level violations | **4,113** |
| In rules this agent can patch | **2,430 (59.1%)** |
| Median per site | **13** |

These are not abandoned websites. *Writing an OS in Rust*, with over 17,000
stars, has 67 violations this agent can fix. Fastlane's documentation has 99.
Ionic's has 39.

The harness that produced those numbers is in the repository as
`bench/scan_fleet.py`, with the raw results, so the figure is reproducible
rather than asserted. One honest caveat: the denominator is *reachable*
sites. 69 of the 199 failed to load, mostly because the target list
constructs a `github.io` URL for repositories that declare no homepage, so a
share of those are bad guesses rather than dead hosts.

There's also a clock on this now. The US Justice Department's Title II rule
under the ADA makes WCAG 2.1 AA a legal requirement for state and local
government web content — phased in by population served: April 26, 2027 for
larger entities, April 26, 2028 for the rest.

## What it does

The agent watches a GitHub repository. When a pull request lands, a Cloud Run
Job clones the PR's head branch, renders it with a real headless browser, and
scans it with axe-core. For every violation in a rule it knows how to patch —
`image-alt`, `button-name`, `link-name`, `color-contrast`, `label` — it sends
Gemini the exact source line plus a full-page screenshot and asks for a
single-line, reversible fix.

Then it does the part almost nobody else does: it re-renders and re-scans to
check that the fix actually resolved the violation, before the patch is
allowed anywhere near a pull request. Only patches that survive that check
ship. Everything else gets reverted and listed in a triage section for a
human. The output is a pull request, not a runtime overlay that patches the
DOM in the visitor's browser — it changes your actual source, and a person
still reviews and merges every line.

## What was genuinely hard

**Per-patch verification isn't enough — you need a final gate.** The obvious
design re-scans after each patch and calls it verified if the target
violation is gone. That's necessary but not sufficient: its baseline is
frozen at the start of the run, so it has no way of noticing that patch #4
quietly re-broke something patch #1 already fixed. It also can't tell that
two different patches rewrote the same source line and only one of them
actually survived on disk. The fix was a second, independent check: after
every violation has been through the propose/apply/verify loop, one more
whole-page re-scan runs against the live page, and the run's result is
compared by *count*, not by set membership — a rule+selector pair the
baseline already had can show up once in the final scan and still be one
*more* than the run should have left behind, if the fixed one reappeared or a
second node now carries the same identity. A set comparison would miss both.
This is the only check in the codebase allowed to promote a patch into
`RunResult.verified`, and it's the reason the demo can show a fix that gets
proposed, applied, and then reverted in the same run when it doesn't hold up.

**`git commit -am` will ship whatever is on disk, verified or not.** A revert
that fails for any reason leaves a rejected patch sitting in the working tree
right next to the ones that passed. A naive commit step would sweep it into
the PR under one "fixed" banner with no way to tell which lines were actually
proven. The worker refuses to commit at all if the tree can't be shown to be
exactly the verified fixes — no partial PR, no PR at all, the run is recorded
as unsafe with why.

**Two separate credential leaks into the logs, caught by actually running the
job.** The first pass redacted the raw `GITHUB_TOKEN` from anything the
worker could raise. The very next real run leaked the *same* token again —
this time as the base64-encoded `Authorization: Basic ...` header built from
it, a different string containing no literal copy of the original, so the
single-secret redaction never matched it. It reached Cloud Logging in
plaintext (base64 is not encryption). The fix generalized redaction to take
every secret-shaped value in play, not one. This surfaced from real
end-to-end runs against a real PR, not from unit tests, and it's the kind of
bug that only exists once you actually deploy and watch the logs.

**Things that only break inside a container.** `python:3.13-slim` has no
`git`, and the `playwright` pip package is only the driver — the Chromium
binary is a separate download the Dockerfile never asked for. Both are
load-bearing for the worker and neither had ever been exercised before a
real job execution, because the *service* half of this deployment doesn't
shell out to git or launch a browser — its own successful deploy gave no
signal that either was missing. Running `python job/worker.py` directly also
couldn't import `app`, even though `uvicorn app.main:app` on the exact same
image could — running a script by path sets `sys.path[0]` to the script's
own directory, not the working directory, so `app/` never resolved. Fixed
with `PYTHONPATH=/app` in the Dockerfile. Cloud Run Jobs execution overrides
also replace the container's `args` list wholesale rather than appending to
it — a deploy that put the entrypoint script path inside `args` worked with
no override and broke on every real invocation, since the whole point of an
override is to pass the per-run JSON payload. Moving the script path into
`command` fixed it permanently, because an override can then only ever
replace the payload, never the entrypoint.

**GitHub's git-over-HTTPS endpoint doesn't take the header its own REST API
takes.** `GET /user` with `Authorization: Bearer <token>` returns 200; `git
clone` with the same header gets `remote: invalid credentials`. HTTP Basic
auth (`x-access-token:<token>`, base64-encoded) is what git actually wants.
And `git clone -c http.extraheader=...` persists that header into the
clone's own `.git/config` — a later `git push` repeating the same flag sends
the header twice, and GitHub answers with a plain 400 that git only reports
as a bare non-zero exit. Fixed by setting the header once, at clone, and
letting push inherit it.

## The architecture, briefly

GitHub webhook → Pub/Sub → a thin FastAPI service that parses the event and
starts a Cloud Run Job execution → the job clones the PR head, serves it
locally, and runs the scan → locate → propose → apply → verify loop per
violation, followed by the final whole-page gate. Every step is written to
Firestore as it happens, not just at the end, and a small vanilla-JS console
renders that trail per run. Cloud Trace carries one span per
scan/propose/apply/verify call; Cloud Logging carries one structured JSON
line per event. If the gate passes, the worker commits, pushes to a new
branch, and opens a pull request listing every fix with its rationale and
every violation it couldn't resolve.

## What it does not do

It is not an overlay — nothing runs in a visitor's browser, and every change
still goes through a pull request a human reviews and merges. Automated
rules only cover a subset of WCAG: axe-core catches what's machine-
detectable, and this agent patches five specific rule families out of what
it scans for. Anything that needs human judgment — is this alt text actually
descriptive of *this* image, does this heading order make sense — goes into
the triage list instead of being guessed at. And it will not ship a fix it
cannot prove: if the final verification scan can't run, or a rejected patch
is stuck on disk, the run produces no pull request at all rather than one
that mixes verified and unverified changes under a single "fixed" banner.

## It runs on code it has never seen

The demo fixture is a page built to contain exactly the defects the agent
knows how to fix, which proves the loop works and proves nothing about the
world. So we forked three real open-source projects and ran it against them.

| Repository | Violations | Verified | Triaged |
|---|---|---|---|
| a landing page template | 67 | 23 | 44 |
| Dopefolio, a portfolio template | 33 | 13 | 20 |
| a personal portfolio | 27 | 4 | 23 |

Each opened a real pull request. The verified fixes on the first spanned
three rule types — `button-name`, `color-contrast` and `image-alt` — on
markup nobody wrote for us.

The triage numbers are the honest part. On that first run, 39 of the 44 were
rules this agent does not attempt at all, and 5 were ones where the model
could not produce a usable edit. It fixed what it could prove and handed back
the rest, labelled.

103 tests back this, all running against fakes and a real local headless
browser scan of the checked-in fixture — no cloud credentials required to run
the suite.
