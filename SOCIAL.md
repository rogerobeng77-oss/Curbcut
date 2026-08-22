# Social posts — a11y-agent

## X (under 280 characters)

95.9% of the top 1M home pages fail automated WCAG checks (WebAIM Million,
Feb 2026) — up from 94.8%. My agent watches a repo's PRs, patches real
source for accessibility violations, then re-scans to *prove* each fix
before it opens a PR. #AllThingsAgenticHackathon

(char count: ~276)

## LinkedIn

95.9% of the top one million home pages on the web have at least one
detectable accessibility failure — that's the WebAIM Million's February 2026
finding, and it's *up* from 94.8% the year before, reversing six years of
slow progress. The tools to catch these violations have been free for years.
Detection was never the bottleneck — remediation capacity was.

I built an agent for the All Things Agentic Hackathon that closes that gap.
It watches a GitHub repo, and when a pull request lands, it renders the page,
finds WCAG violations with axe-core, and asks Gemini for a real source patch
— not a runtime overlay. Then it does the part almost nobody else does: it
re-renders and re-scans to prove the fix actually worked, using a whole-page
gate at the end of the run specifically because a violation can look "fixed"
against a stale per-patch baseline while a later change quietly re-breaks it.
Only patches that survive that proof ever reach a pull request; everything
else is reverted and handed to a human as a triage list.

It's not an overlay, and it doesn't merge itself — a person reviews every
change. But the fixes it does propose come with proof, not just a suggestion.
96 tests, deployed on Cloud Run, with a live demo PR against a fixture repo.

Built and written up for the All Things Agentic Hackathon.
#AllThingsAgenticHackathon
