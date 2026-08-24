## Inspiration

Every year WebAIM scans the home pages of the top one million sites. The February 2026 run found that 95.9% of them carry at least one WCAG failure a machine can detect by itself. Missing alt text. A button with no name. Text you cannot read against its own background. And the figure went up from the year before, after six straight years of slowly coming down.

That is the part that troubled me. These are not hard things to find. axe-core is free, it has been free for years, and it will tell you the exact element and the exact rule it broke. Detection is a solved problem. What nobody has solved is somebody sitting down to fix the 40 items the scanner listed.

Then there is the deadline. The US Justice Department's Title II rule makes WCAG 2.1 AA a legal requirement for state and local government web content. April 2027 if you serve 50,000 people or more, April 2028 for everybody else. So plenty of small district offices are going to run a scanner in 2026, see 200 violations, and have nobody to hand the list to.

Most of the tools we have make the report longer. The overlay products go the opposite way and patch the page inside the visitor's browser, which never touches your source, so your repository is still broken and the next deploy takes you back to where you started. I wanted the thing that opens a pull request.

## What it does

Curbcut watches a GitHub repository. When a pull request is opened or updated, a Cloud Run Job clones that branch, serves it locally, and scans it with a real headless Chromium and axe-core.

For every violation in a rule it knows how to patch (`image-alt`, `button-name`, `link-name`, `color-contrast`, `label`) it asks Gemini for a single-line source fix, applies it to the file, and re-renders and re-scans to see whether that violation actually went away and whether the patch broke something else.

After all of them have been through that loop it runs one more scan of the whole page. That last one is the only check that can catch a later patch quietly undoing an earlier one, and it is the only thing in the codebase allowed to mark a patch as verified.

If the working tree cannot be shown to be exactly those verified patches, no commit happens and no pull request opens. The run is recorded as unsafe with the reason. Otherwise it commits, pushes a branch, and opens a PR that lists every fix with its rationale and every violation it could not handle, so a human can pick those up.

Every step writes to a Firestore audit trail while it is happening, and a small console replays the trail run by run. Nothing runs in the visitor's browser. A person still reviews and merges every single change.

The honest limit: axe-core only catches what is machine-detectable. Whether alt text actually describes *this* image, whether the heading order makes sense, whether the reading order is right, all of that needs a human. Curbcut puts those in the triage list instead of guessing.

## How we built it

The worker is Python. Playwright drives Chromium, axe-core 4.13.0 does the scanning, and Gemini 3.5 Flash on Vertex AI proposes the patches through the Google GenAI SDK. FastAPI handles webhook intake and serves the console and read API.

GitHub sends the pull request event, Pub/Sub relays it to a Cloud Run service, and that service starts a Cloud Run Job execution with the payload. I split it that way on purpose: a webhook has to answer immediately, but a scan-patch-rescan loop over a real browser takes minutes. Firestore holds the audit trail and run records. Cloud Trace gets the spans through OpenTelemetry. PyGithub opens the PR.

Underneath all three of my hackathon projects is a shared substrate I wrote first, holding config, safety guards, the Firestore wrapper, the Gemini client, the Pub/Sub and Jobs plumbing, telemetry, and test fakes. I copied it into each repository rather than publishing it as a package, so anybody can clone one repo and run it without hunting for a dependency.

The console is plain HTML, CSS and JavaScript. No framework. I scanned it with the product's own scanner and it reports zero violations, which felt like the least I could do.

96 tests, all running against fakes and a local scan of a checked-in broken fixture, so the suite needs no GCP credentials at all.

## Challenges we ran into

The model version nearly finished me. `gemini-3.5-flash` is served only on the Vertex location `global`. Every regional endpoint returns 404. And `gemini-2.5-flash` works regionally without complaint, which is the trap, because it runs perfectly on your machine and quietly fails the hackathon's version requirement.

Deploying is where the real lessons came. The first genuine run against a real PR failed five times, and each failure was something no test could have caught because it only exists inside a container:

`python:3.13-slim` ships with no `git`, and the `playwright` pip package is only the driver, not the Chromium binary. Both are load-bearing for the worker and neither had ever been exercised, because the web service does not shell out to git or launch a browser, so its own successful deploy gave me no signal.

`python job/worker.py` could not import `app` even though `uvicorn app.main:app` could, on the same image. Running a script by path puts the script's own directory on `sys.path`, not the working directory.

Cloud Run Jobs execution overrides replace `args` wholesale instead of appending. My deploy had the entry-point script path sitting in `args`, so it worked when executed plain and broke the moment anything passed an override, which is every real invocation.

GitHub rejects a raw `Authorization: Bearer` header for git-over-HTTPS even when the same token and the same header work fine against the REST API. It wants HTTP Basic. Then `git clone -c http.extraheader` writes that header into the clone's own config, so a later push repeating it sends it twice and GitHub answers `Duplicate header: "Authorization"` with a bare 400 that git reports only as a non-zero exit.

And a fresh container has no git identity, so the commit failed with git's own "Please tell me who you are".

The one that still bothers me is the token. I redacted `GITHUB_TOKEN` from anything the worker could raise, and the very next run leaked the same token again, this time as the base64 `Authorization: Basic` header built from it. Different string, no literal copy of the secret inside it, so my single-value redaction never matched. Redaction has to cover every string a secret produces, not the secret alone.

Then there was a design bug I only caught by arguing with myself. Per-patch verification cannot see a later patch undoing an earlier one, because its baseline is frozen at the start of the run. That is why the final whole-page re-scan exists.

## Accomplishments that we're proud of

It ran. Not a demo recording, a real Cloud Run Job execution that finished in 3 minutes 12 seconds and opened a live pull request on GitHub with 7 verified accessibility fixes in it, each one re-rendered and re-scanned before it was allowed near the commit.

The refusal path is the piece I am most pleased with, and it is the part that will never show up in a screenshot. `git commit -am` will ship whatever is lying on disk, verified or not. If a rejected patch fails to revert it sits there next to the good ones and a blind commit puts it in the PR under a "verified" banner. Curbcut refuses to commit at all when that can happen. An agent that knows when to produce nothing is worth more to me than one that always produces something.

The console passing its own scanner with zero violations. Building an accessibility tool with an inaccessible interface would have been a bad look.

And a small thing I like: the test fixture has six `<!-- VIOLATION -->` markers but axe-core reports seven node-level violations across five rules, because one marker, a linked image with no accessible name, trips two separate rules at once. I only know that because I ran a live scan instead of counting the comments.

## What we learned

Deploy early, and deploy the thing that is hardest to deploy first. Six of my bugs existed only inside a container and every one of them was invisible to a green test suite on my laptop.

Test doubles built from what you assume the API does will happily certify your bug. I had a fake that defined a property as a method, so the assertion passed and proved my mistake back to me.

A confidently written comment is not evidence. Reviewing my own work I found fifteen claims stated with full confidence that were simply not true, including a README of mine that said the agent had six halt conditions when the code defines four.

Fixing one thing breaks another in the same file minutes later. I added `tabindex` to an element for keyboard access and threw in `role="log"` at the same time, which stripped the list semantics and orphaned every `<li>`. One serious violation traded for a serious plus a minor. Re-run the check that made you write the fix, after the fix.

And look at the rendered pixels. Seeded demo copy is the only thing in a build with no test coverage and the very thing a judge reads most closely.

## What's next for Curbcut

More rules. Five patchable rule ids is a start, not a finish. `aria-required-attr`, `heading-order` and `region` are next, and each one needs its own proof that a patch is genuinely safe to apply without a human looking.

Framework support beyond static HTML. Right now Curbcut patches source files it can locate directly. React, Vue and templating languages need the agent to find the component that produced the offending node, which is a harder locate step.

A repository-wide first pass. Today it reacts to a pull request. A team inheriting a site with 200 violations wants one PR per batch, prioritised, not a wait for somebody to touch each page.

Screen reader verification. axe-core checks structure. Whether a page is actually usable with NVDA or VoiceOver is a different question, and I would like the verification loop to eventually answer it.

Rotating that GitHub token, which is sitting in my notes in capital letters.
