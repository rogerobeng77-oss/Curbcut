# Demo script — 4:00 maximum

## 0:00–0:30 — The problem, with the source on screen

On screen, before any UI appears: **"95.9% of the top one million home
pages have detectable WCAG failures."** Caption: **"WebAIM Million,
February 2026."**

Say: "Ninety-five point nine percent of the top million home pages fail
automated accessibility checks. Detection has been free for years — every
one of those pages could have been scanned. The reason they're still broken
is nobody has time to do the fixing."

On screen next: **"ADA Title II — WCAG 2.1 AA required for state and local
government sites: April 26, 2027 for larger entities, April 26, 2028 for
the rest."**

Say: "And as of the Justice Department's Title II rule, this is no longer
optional for public-sector sites in the US. There's a clock on it now."

## 0:30–1:00 — What this is

Say: "This agent watches a repository. When a pull request lands, it
renders the page, finds the violations, and writes an actual source patch —
not a runtime overlay. Then — this is the part nobody else does — it
re-renders and re-scans to *prove* the fix worked before it ever reaches a
human. If a fix doesn't hold up, it gets reverted, not shipped."

## 1:00–2:45 — Live run, unedited

Open a real pull request on the fixture repository. Show:
- the webhook landing and the Cloud Run Job execution starting;
- the reasoning chain filling in on the console, step by step
  (scan → locate → propose → apply → verify, per violation);
- the checker going from **six seeded defects — which axe-core resolves
  to seven node-level findings across five rule ids — to zero**;
- the resulting pull request: read one fix's rationale aloud, and show the
  triage section listing anything the run could not resolve.

## 2:45–3:10 — The most important shot: a fix that fails, reverted

Show one patch that does **not** survive verification — either a seeded
regression case or the run's own final-gate log line — and the revert that
follows it in the audit trail. Say: "This is the shot that matters most.
A patch that doesn't hold up gets undone, not shipped next to the ones
that did. `git commit -am` would ship everything on disk without this
check; this agent refuses to commit at all when it can't prove the tree is
clean."

## 3:10–3:35 — Proof it runs on Google Cloud

Show the Cloud Run Jobs execution mid-run in the console, then Cloud
Logging with the structured JSON entries (`run.complete`, `pr.opened`),
then Cloud Trace with the `a11y.run` / `a11y.scan` / `a11y.verify` span
tree for that same run.

## 3:35–4:00 — The honest limit

Say: "Automated rules only cover a subset of WCAG. This clears the
machine-detectable floor and hands everything else to a human, with what
it tried and why it stopped. It patches your source and opens a pull
request — it is not an overlay, and a human merges every change."

Close on the console's triage list.

## Notes for whoever records this

- The violation count is not "six to zero." Six is the number of
  `<!-- VIOLATION -->` markers seeded in `fixture/index.html`; a live scan
  of that fixture reports **seven** node-level axe findings (one marker — a
  linked image with no accessible name — trips both `image-alt` and
  `link-name`). Say seven, and say why, if asked.
- Record the live run once, end to end, before scripting narration around
  it — do not fabricate a run that did not happen.
