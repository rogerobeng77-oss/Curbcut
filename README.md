# Curbcut

Curbcut watches a GitHub repository. When a pull request lands, it renders
the page, finds the accessibility violations, writes a real source patch for
each one it can, and then re-renders and re-scans to check the patch actually
worked. Only patches that survive that check get committed. The rest are
reverted and listed for a person to look at.

The output is a pull request. Nothing runs in your visitor's browser, and a
human merges every change.

Built for the All Things Agentic Hackathon, Taskmaster track.

**Live console:** https://a11y-agent-cxotjai2ta-uc.a.run.app
**A pull request it opened:** https://github.com/rogerkorantenng/a11y-demo-fixture/pull/3

## Why

Detection has been a solved problem for years. axe-core is free, and it will
tell you the exact element and the exact rule it broke. What nobody has is
the time to sit down and fix the forty things it listed.

The WebAIM Million found in February 2026 that 95.9% of the top million home
pages have at least one detectable WCAG failure, up from 94.8% the year
before. We wanted to know whether that held somewhere nobody surveys, so we
pointed this scanner at 199 real project and documentation sites on GitHub
Pages. 130 were reachable. 127 of those had violations:

| | |
|---|---|
| Reachable sites with at least one violation | **127 of 130 (97.7%)** |
| Total node-level violations | **4,113** |
| In rules Curbcut can patch | **2,430 (59.1%)** |
| Median per site | **13** |

Some of the worst are well known: `fastlane/docs` (99 patchable),
`ionic-team/ionic-docs` (39), and `phil-opp/blog_os` (67, with 17,681 stars).

There is also a deadline now. The US Justice Department's Title II rule makes
WCAG 2.1 AA a legal requirement for state and local government web content,
from 26 April 2027 for entities serving 50,000 people or more and 26 April
2028 for the rest.

## How a run works

1. GitHub says a pull request opened or changed. The event arrives through
   Pub/Sub.
2. A Cloud Run Job clones that branch, serves it locally, and scans it with
   headless Chromium and axe-core.
3. For each violation in a rule it knows (`image-alt`, `button-name`,
   `link-name`, `color-contrast`, `label`) it asks Gemini for a one-line
   source fix, applies it, and re-scans to check that violation is gone and
   nothing new appeared.
4. After all of them, it scans the whole page once more. This is the only
   check that can catch a later patch quietly undoing an earlier one, and it
   is the only thing allowed to mark a patch verified.
5. If the working tree cannot be shown to be exactly those verified patches,
   nothing is committed and no pull request opens. The run is recorded as
   unsafe, with the reason.
6. Otherwise it commits, pushes a branch, and opens a pull request listing
   every fix with its rationale and everything it could not resolve.

Every step is written to a Firestore audit trail as it happens, and the
console replays it run by run.

## What it does not do

**It is not an overlay.** It patches your source and opens a pull request.
A human merges every change.

**It does not cover all of WCAG.** axe-core catches what a machine can
detect. Whether alt text actually describes *this* image, whether the heading
order makes sense, whether the reading order is right — all of that needs a
person, and goes into the triage list rather than being guessed at.

**It will not ship a fix it cannot prove.** If the final scan cannot run, or
a rejected patch is stuck on disk, the run produces no pull request at all
rather than one that mixes verified and unverified changes under a single
"fixed" banner.

## Run it yourself

You need Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and for the deploy
steps a Google Cloud project with Firestore, Pub/Sub, Cloud Run, Vertex AI
and Cloud Trace enabled.

### Install and test

```bash
git clone <this-repo-url> curbcut && cd curbcut
uv pip install -e ".[dev]"
uv run playwright install --with-deps chromium
uv run pytest -q
```

97 tests. They run against `substrate.fakes` and a real local browser scan of
the checked-in `fixture/`, so you need no Google Cloud credentials.

If you see `ModuleNotFoundError: No module named 'substrate'`, run
`uv pip install -e ".[dev]"` again. `uv run` sometimes skips the editable
install.

### Run the console locally

```bash
export USE_FAKE_STORE=1
uv run uvicorn app.main:app --reload
```

`USE_FAKE_STORE` swaps in an in-memory Firestore and sends traces nowhere, so
this works with no cloud account. Open http://127.0.0.1:8000/.

### Reproduce the 97.7%

The number above is a measurement this repo makes, not a citation. You can
re-make it:

```bash
uv run python bench/scan_fleet.py bench/targets.json /tmp/out.jsonl 20
```

That scans the first 20 targets. Drop the number to run all 199.
`bench/results.jsonl` and `bench/summary.json` hold the run quoted above.

Two things to know before quoting a figure from it. The denominator is
*reachable* sites: 69 of the 199 failed, largely because `targets.json`
guesses a `github.io` URL for repos that declare no homepage, so some of
those are bad guesses rather than dead hosts. And counts are node-level, the
same unit `app/scanner.py` reports during a real run.

### Deploy

```bash
export GCP_PROJECT=<your-project>

gcloud pubsub topics create a11y-events --project=$GCP_PROJECT
gcloud secrets create github-token --data-file=- <<< "$(gh auth token)"

./deploy.sh a11y-agent

SERVICE_URL=$(gcloud run services describe a11y-agent \
  --project=$GCP_PROJECT --region=us-central1 --format='value(status.url)')
gcloud pubsub subscriptions create a11y-events-push \
  --topic=a11y-events --push-endpoint="${SERVICE_URL}/events" --project=$GCP_PROJECT

gcloud run jobs deploy a11y-worker \
  --source . --project=$GCP_PROJECT --region=us-central1 \
  --command=python,job/worker.py \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=true,GCP_PROJECT=$GCP_PROJECT,GCP_LOCATION=us-central1" \
  --set-secrets="GITHUB_TOKEN=github-token:latest" \
  --max-retries=1 --task-timeout=900
```

The script path goes in `--command`, not `--args`. Cloud Run Jobs execution
overrides replace `args` wholesale, so a script path living in `args` gets
dropped by the very override that passes the per-run payload.

The service account needs `roles/aiplatform.user`, `roles/datastore.user`,
`roles/cloudtrace.agent` and `roles/secretmanager.secretAccessor`:

```bash
SA=$(gcloud run services describe a11y-agent --project=$GCP_PROJECT \
  --region=us-central1 --format='value(spec.template.spec.serviceAccountName)')
for ROLE in roles/aiplatform.user roles/datastore.user \
            roles/cloudtrace.agent roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding $GCP_PROJECT \
    --member="serviceAccount:${SA}" --role="$ROLE"
done
```

### Trigger a run

Point the repository's webhook at something that republishes to the
`a11y-events` topic, or publish an event directly:

```bash
gcloud pubsub topics publish a11y-events --project=$GCP_PROJECT \
  --message='{"action":"opened",
              "pull_request":{"number":1,"head":{"ref":"my-branch","sha":"<sha>"}},
              "repository":{"full_name":"owner/repo"}}'
```

## Built with

Python 3.13 on Cloud Run. FastAPI and uvicorn for the webhook and console.
Playwright with Chromium and axe-core 4.13.0 for scanning. The Google GenAI
SDK against Vertex AI (`gemini-3.5-flash`, which is only served on the
`global` location) for patch proposals. Firestore for the audit trail,
Pub/Sub between the webhook and the job, Cloud Run Jobs for the work itself,
and Cloud Trace through OpenTelemetry. PyGithub opens the pull request.

The console is plain HTML, CSS and JavaScript. No framework. It scans clean
against this product's own scanner.

## Data sources

- **The page under remediation**, rendered with Playwright. Nothing else from
  the repository is retained beyond the lifetime of one run's temporary
  checkout.
- **axe-core's rule set** — accessibility violations, not user data.
- **The GitHub pull request event** — repository, PR number, head branch and
  head SHA.
- **`bench/targets.json`** — 199 public repositories with a published site,
  collected from the GitHub search API. Only their public HTML is fetched.

Nothing is collected about the end users of any scanned site. This agent acts
on source code, not on traffic. No personal data is stored, and the Firestore
records hold run metadata, patch diffs and the audit trail only.

## More

- [ARCHITECTURE.md](ARCHITECTURE.md) — the diagram and how the pieces fit
- [FINDINGS.md](FINDINGS.md) — the bugs that only showed up in production,
  and the design mistakes we caught by arguing with our own code
- [BLOG.md](BLOG.md) — the longer write-up
