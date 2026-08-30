# What broke, and what it taught us

These are the bugs that only appeared in production, plus the design
mistakes we caught by arguing with our own code. They are kept out of the
README so it stays readable, but they are the honest record of how this
was built.

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
- **GitHub's git-over-HTTPS endpoint rejects a raw `Authorization: Bearer
  <token>` header, even for a token valid for the REST API with that exact
  header.** `GET /user` returned `200` with `Authorization: Bearer <token>`;
  `git clone` with the same header on `-c http.extraheader` got `remote:
  invalid credentials`. HTTP Basic auth (`x-access-token:<token>`,
  base64-encoded) is what GitHub's own git integration actually accepts.
- **`git clone -c http.extraheader=...` persists that header into the
  clone's own `.git/config`.** A later `git push` that repeats the same
  `-c http.extraheader` therefore sends the header *twice* — GitHub answers
  with `remote: Duplicate header: "Authorization"` and a plain `400`, which
  git itself only reports as a bare non-zero exit. Found by reproducing the
  clone-then-push sequence by hand outside the container after the real job
  failed with no more specific a message. Fixed by setting the header once,
  at clone, and letting push inherit it.
- **A fresh container has no git identity, and `git clone` does not
  create one.** `git commit` failed with git's own "Please tell me who you
  are" (exit 128) on the first real run that got that far. Fixed by scoping
  `-c user.name=... -c user.email=...` to the one commit, the same way the
  auth header is scoped rather than written to a global config this
  container does not own.
- **Redacting a secret from logs has to cover every string that secret
  produces, not just the secret itself — found by leaking the same token
  twice.** The first fix redacted the raw `GITHUB_TOKEN` from anything
  `job/worker.py` could raise. The very next real run leaked the *same*
  token again, this time as the base64-encoded `Authorization: Basic ...`
  header built from it — a different string that contains no literal copy
  of the token, so the single-secret redaction never matched it, and it
  reached Cloud Logging in plain (if base64-"encoded," which is not
  encryption) text. `_redact` and `_run_git` now take every secret-shaped
  value in play (`*secrets`), not one. **The `GITHUB_TOKEN` value currently
  stored in the Secret Manager `github-token` secret should be rotated
  before this project is demoed or submitted further.** It appeared in
  plaintext (once as a raw token, once as base64, in two separate log
  entries) during this batch's own deploy testing, before the redaction fix
  landed — anyone with Cloud Logging read access to this GCP project could
  read it from the log history. It was not revoked by this batch, since
  doing so is an action on the project owner's own GitHub credential that
  this batch did not have standing to take unilaterally; see the report for
  the exact log entries and timestamps.
