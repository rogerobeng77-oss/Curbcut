# a11y-agent

An agent that watches a GitHub repository for pull requests, renders each
page, finds WCAG accessibility violations with axe-core, writes a real
source patch for each one it can, and then **proves the patch worked** by
re-rendering and re-scanning before it ever reaches a human. Only patches
that survive that proof are committed; everything else is reverted and
listed for a person to look at. The output is a pull request, not a
runtime overlay — a human still reviews and merges every change.

Built for the All Things Agentic Hackathon (Taskmaster track).

## Why

The WebAIM Million (February 2026) found that 95.9% of the top one million
home pages have at least one detectable WCAG failure — automated detection
has been free and available for years, and the number is not falling. The
US Justice Department's Title II rule makes WCAG 2.1 AA a legal requirement
for state and local government web content, phased in by entity size
(2027-04-26 for entities serving populations of 50,000 or more, 2028-04-26
for the rest). Detection was never the bottleneck; remediation capacity was.
This agent turns detection into a merge-ready patch instead of a report
nobody has time to act on.

## What it does

1. A GitHub webhook (relayed through Pub/Sub) tells the service a pull
   request was opened, reopened, or updated.
2. A Cloud Run Job execution clones that PR's head branch, serves it
   locally, and scans it with a real headless browser and axe-core.
3. For each violation in a rule this agent knows how to patch
   (`image-alt`, `button-name`, `link-name`, `color-contrast`, `label`), it
   asks Gemini for a single-line source fix, applies it, and re-scans to
   check the fix actually resolved that violation without introducing a new
   one.
4. After every violation has been through that loop, it does **one more**
   whole-page re-scan — the only check that can catch a later patch quietly
   undoing an earlier one — and only patches that survive it are kept.
5. If the working tree cannot be shown to be exactly those verified
   patches (a rejected patch that could not be reverted, or the final scan
   itself failing to run), **no commit happens and no pull request opens.**
   The run is recorded as unsafe, with why.
6. Otherwise it commits, pushes to a new branch, and opens a pull request
   that lists every fix with its rationale and every violation it could not
   resolve, for a human to handle.
7. Every step — scan, locate, propose, apply, verify, the final gate — is
   written to a Firestore audit trail as it happens, and a small console
   renders that trail per run.

## What it does not do

- **It is not an overlay.** Nothing runs in the visitor's browser. It
  patches your actual source and opens a pull request; a human merges
  every change.
- **Automated rules cover only a subset of WCAG.** axe-core catches what is
  machine-detectable — missing alt text, unlabeled form fields, insufficient
  contrast, unnamed buttons and links, and more it scans for but this agent
  does not yet patch. Anything that needs human judgment (is this alt text
  actually descriptive of *this* image, does this heading order make sense,
  is this really the right reading order) is out of scope by construction
  and goes into the triage list instead of being guessed at.
- **It will not ship a fix it cannot prove.** If the final verification
  scan can't run, or a rejected patch is stuck on disk, the run produces no
  PR at all rather than a PR that mixes verified and unverified changes
  under one "fixed" banner.

## Spin-up instructions (from a clean machine)

Prerequisites: Python 3.11+, [`uv`](https://docs.astral.sh/uv/), a GCP
project with Firestore (Native mode), Pub/Sub, Cloud Run, Cloud Run Jobs,
Vertex AI, and Cloud Trace enabled, and `gcloud` authenticated against it.

### 1. Clone and install

```bash
git clone <this-repo-url> a11y-agent && cd a11y-agent
uv pip install -e ".[dev]"
uv run playwright install --with-deps chromium
```

`uv run` can silently skip the editable install on some setups — if you
see `ModuleNotFoundError: No module named 'substrate'` (or `'app'`, or
`'job'`), re-run `uv pip install -e ".[dev]"` before doing anything else.

### 2. Run the tests

```bash
uv run pytest -q
```

All tests use `substrate.fakes.FakeFirestore` / `FakeModel` and a real (but
local) headless-browser scan of the checked-in `fixture/` — no GCP
credentials are needed to run the suite.

### 3. Run the service locally

```bash
export USE_FAKE_STORE=1   # in-memory Firestore stand-in; skips Cloud Trace export too
uv run uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/` for the console (empty until a run exists).
`GET /healthz`, `POST /events`, and the `/api/runs*` routes are all live.

### 4. Deploy to Cloud Run

```bash
# One-time infrastructure
gcloud pubsub topics create a11y-events --project=<PROJECT>
gcloud secrets create github-token --data-file=- <<< "$(gh auth token)"   # a GitHub token with repo + pull-request scope

# The service (webhook intake + console)
./deploy.sh a11y-agent
SERVICE_URL=$(gcloud run services describe a11y-agent \
  --project=<PROJECT> --region=us-central1 --format='value(status.url)')
gcloud pubsub subscriptions create a11y-events-push \
  --topic=a11y-events --push-endpoint="${SERVICE_URL}/events" --project=<PROJECT>

# The worker (one execution per PR event)
# NOTE: the script path goes in --command, not --args. Cloud Run Jobs
# execution overrides (and substrate.events.enqueue_job, which is how the
# service actually starts a job) replace `args` wholesale — if the script
# path is itself in `args`, an override drops it and the container tries to
# `python <json>` and fails. Baking it into `command` means an override can
# only ever replace the JSON payload, never the entrypoint.
gcloud run jobs deploy a11y-worker \
  --source . --project=<PROJECT> --region=us-central1 \
  --command=python,job/worker.py \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=true,GCP_PROJECT=<PROJECT>,GCP_LOCATION=us-central1" \
  --set-secrets="GITHUB_TOKEN=github-token:latest" \
  --max-retries=1 --task-timeout=900
```

The runtime service account needs `roles/aiplatform.user`,
`roles/datastore.user`, `roles/cloudtrace.agent`, and
`roles/secretmanager.secretAccessor`. Grant them once:

```bash
SA=$(gcloud run services describe a11y-agent --project=<PROJECT> --region=us-central1 \
  --format='value(spec.template.spec.serviceAccountName)')
for ROLE in roles/aiplatform.user roles/datastore.user roles/cloudtrace.agent roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding <PROJECT> --member="serviceAccount:${SA}" --role="$ROLE"
done
```

No Application Default Credentials file is needed at runtime — the
deployed service and job both authenticate as this service account
automatically. Locally, `GITHUB_TOKEN` is not read from Secret Manager;
export it by hand (`export GITHUB_TOKEN=$(gh auth token)`) before running
`job/worker.py` directly, and expect `setup_telemetry` to need real ADC
(`gcloud auth application-default login`) unless `USE_FAKE_STORE=1` is set,
which routes spans to a local no-op exporter instead.

### 5. Point a real webhook at it

Configure the repository's webhook (or a GitHub App) to POST `pull_request`
events at a Pub/Sub-fronted endpoint that republishes to the `a11y-events`
topic, or publish directly for a manual test:

```bash
gcloud pubsub topics publish a11y-events --project=<PROJECT> \
  --message="$(python3 -c 'import json,sys; print(json.dumps({
    "action":"opened",
    "pull_request":{"number":1,"head":{"ref":"my-branch","sha":"<sha>"}},
    "repository":{"full_name":"owner/repo"}}))')"
```

## Technologies

- **Python 3.13** (Cloud Run deploy target; developed against 3.11)
- **FastAPI** + **uvicorn** — webhook intake, read API, static console
- **Playwright** (Chromium) + **axe-core 4.13.0** — rendering and scanning
- **Google Vertex AI** (`gemini-3.5-flash`, `global` endpoint) — patch proposals
- **Google Cloud Firestore** — audit trail and run records
- **Google Cloud Pub/Sub** — webhook-to-job decoupling
- **Google Cloud Run** (service + Jobs) — hosting
- **Google Cloud Trace / Cloud Logging** via OpenTelemetry — observability
- **PyGithub** — pull request creation
- Vanilla HTML/CSS/JS console — no framework, no third-party branding, WCAG
  2.1 AA verified with the product's own scanner

## Data sources

- The page under remediation itself (rendered via Playwright).
- axe-core's rule set (accessibility violations, not user data).
- The GitHub pull request event (repo, PR number, head branch, head SHA) —
  no repository content is retained beyond the lifetime of one run's
  temporary checkout.
- Nothing about end users of the target site is collected; this agent acts
  on source code, not on traffic.

## Findings and learnings

- **The seeded-defect count is not the finding count.** The fixture has six
  `<!-- VIOLATION --> ` markers, but axe-core reports **seven** node-level
  violations across five rule ids against it (`button-name` 1,
  `color-contrast` 1, `image-alt` 2, `label` 1, `link-name` 2) — one marker
  (a linked image with no accessible name) trips two separate rules at
  once. Verified with a live scan of the committed fixture, not assumed
  from the source comments.
- **Verifying "at the time" is not the same as verifying "at the end."**
  Per-patch verification alone cannot see a later patch quietly undo an
  earlier one, because its baseline is frozen when the run starts. Only a
  final whole-page re-scan after every patch has been applied can — and it
  is the only thing in this codebase allowed to promote a patch to
  `verified`.
- **`git commit -am` will ship whatever is on disk, verified or not.** A
  revert that fails leaves a rejected patch sitting in the working tree
  next to the verified ones, and a blind `commit -am` would put it in the
  PR under a "verified" banner. The worker refuses to commit at all when
  that can happen (`RunResult.safe_to_ship`).
- **Cloud Run Jobs execution overrides replace `args`, not append to it.**
  `substrate.events.enqueue_job` and `gcloud run jobs execute --args` both
  replace the container's `args` list wholesale. A deploy that puts the
  entry-point script path in `args` (as an earlier version of this job did)
  works when executed with no override and breaks the moment anything
  overrides `args` — which is every real invocation, since the whole point
  of the override is to pass the per-run JSON payload. Found by actually
  executing the job against a real PR, not by reading the deploy script.
  Fixed by moving the script path into `command`.
- **`setup_telemetry()` with no explicit span processor needs live ADC at
  *import* time**, not just at export time — `CloudTraceSpanExporter`'s
  constructor calls `google.auth.default()` immediately. That makes
  `app.main` unimportable on any machine without Application Default
  Credentials, including this project's own test suite. Fixed by routing
  to a local no-op span exporter whenever `USE_FAKE_STORE` is set (the
  project's existing signal for "no live GCP credentials assumed").
- **`python job/worker.py` cannot import `app`, even though `uvicorn
  app.main:app` (the service's own entrypoint, same image) can.** Running a
  script by path sets `sys.path[0]` to that script's *own* directory
  (`/app/job`), not the working directory — unlike `-c` or `-m`, which put
  `''` (the cwd) on `sys.path` and let `app/` resolve directly. Confirmed
  live with a throwaway diagnostic execution (`python -c "import sys;
  print(sys.path); import app"` succeeded; `python job/worker.py` failed
  with `ModuleNotFoundError: No module named 'app'` on the same image).
  Fixed with `ENV PYTHONPATH=/app` in the Dockerfile, which every
  invocation style honours.
- **`python:3.13-slim` has no `git`, and the `playwright` pip package is
  only the driver — the Chromium binary is a separate download the
  Dockerfile never asked for.** Both are load-bearing for the worker (`git
  clone`/`commit`/`push` in `job/worker.py`; `playwright.chromium.launch()`
  in `app/scanner.py`) and neither had ever been exercised before this
  batch — the service doesn't shell out to git or launch a browser, so
  its own deploy gave no signal that either was missing. Both surfaced as
  `FileNotFoundError` / a missing-executable error on the first real job
  execution against a real PR. Fixed with `apt-get install git` and
  `playwright install --with-deps chromium` in the Dockerfile.
- **`/healthz` does not reach the container on the public `*.run.app`
  URL.** Confirmed live: a request to `/healthz` gets a Google Frontend
  error page with no `x-cloud-trace-context` header, while `/api/runs` and
  even a genuinely unmapped path on the same domain both get that header,
  meaning `/events` and the console's own API routes reached the FastAPI
  app and `/healthz` never did. Harmless for this deployment — Cloud Run's
  own startup/liveness probing is a TCP check against the container port
  by default and does not depend on this route — but worth knowing before
  wiring an external uptime check to it.
