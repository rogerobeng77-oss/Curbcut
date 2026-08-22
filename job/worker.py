"""Cloud Run Job entry point: clone a PR's head, run remediation against it,
and — only when the run can be trusted — commit the verified fixes and open a
pull request.

This module is invoked as a script (``python job/worker.py '<json-args>'``),
not imported as a library, so most of it is orchestration around real
subprocesses, a real filesystem, and real network calls that this repo has no
seam for testing without turning the test suite into an integration harness.
What *is* pure logic — the safe-to-ship gate and the shape of the record this
job writes to Firestore — is factored out below and covered by
tests/test_worker.py.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from app.github_io import PullRequestRef, open_fix_pr
from app.runner import RunResult, run_remediation
from substrate.config import load_config
from substrate.fakes import FakeFirestore
from substrate.store import Store
from substrate.telemetry import log_event, setup_telemetry


def unsafe_reasons(result: RunResult) -> list[str]:
    """Human-readable reasons ``result`` cannot be trusted enough to ship.

    Empty exactly when ``result.safe_to_ship`` is True (see
    app/runner.py::RunResult.safe_to_ship, and test_unsafe_reasons_is_empty
    _exactly_when_safe_to_ship_is_true, which pins that equivalence). A bare
    boolean is enough for the commit gate; a run that produced no PR needs
    more than that for a human reading the console or the job's own logs.

    Ruling 1 (batch A4): the brief's ``git commit -am`` sweeps the whole
    working tree, so a rejected patch that could not be reverted
    (``unreverted``) would ship unverified, rejected code beside the verified
    fixes -- under a verified banner. That is the failure this function
    exists to name before anything is committed.
    """
    reasons = []
    if not result.final_scan_ok:
        reasons.append("the final whole-page re-scan did not complete")
    if result.reappeared:
        reasons.append(
            f"{len(result.reappeared)} violation(s) reappeared, or were left "
            "unaccounted for, after this run's own patches"
        )
    if result.unreverted:
        reasons.append(
            f"{len(result.unreverted)} rejected patch(es) could not be "
            "reverted -- the working tree carries unverified edits"
        )
    return reasons


def run_id_for(repo: str, pr: int) -> str:
    return f"{repo.replace('/', '_')}-{pr}"


def build_run_record(
    run_id: str, args: dict, result: RunResult, status: str, pr_url: str | None = None
) -> dict:
    """The document this job writes to the `runs` collection.

    Every field the console (app/main.py's /api/runs routes, web/console.js)
    can render is written here, including the fields RunResult grew past the
    original brief -- ruling 3 (batch A4): a run that left the tree modified
    or an incomplete audit trail must say so where a human sees it, and the
    console is the only place a human looks at a run after the fact.
    """
    return {
        "id": run_id,
        "repo": args["repo"],
        "pr": args["pr"],
        "status": status,
        "fixed": len(result.verified),
        "pr_url": pr_url,
        "safe_to_ship": result.safe_to_ship,
        "tree_modified": result.tree_modified,
        "audit_complete": result.audit_complete,
        "reappeared": len(result.reappeared),
        "unreverted": len(result.unreverted),
        "dropped_audit": len(result.dropped_audit),
        "triaged": len(result.triaged),
    }


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "[REDACTED]") if secret else text


def _git_auth_header(token: str) -> str:
    """The `http.extraheader` value that authenticates a git-over-HTTPS
    request against GitHub with `token`.

    Verified live, the hard way: `Authorization: Bearer <token>` -- valid
    for GitHub's REST API with this exact token (`GET /user` returned 200)
    -- is rejected by GitHub's git-over-HTTPS smart endpoint with `remote:
    invalid credentials`. HTTP Basic auth with a conventional placeholder
    username (`x-access-token`) and the token as the password is what
    GitHub's own git integration guides document, and is what actually
    worked in the same reproduction.
    """
    import base64

    return "Authorization: Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()


def _run_git(args: list[str], token: str) -> None:
    """subprocess.run(args, check=True), with the GitHub token scrubbed from
    whatever this raises.

    Verified live and the hard way: an unhandled `CalledProcessError` from a
    `git clone`/`push` invoked with `-c http.extraheader=Authorization:
    Bearer <token>` prints its own `.cmd` in the default traceback, which
    contains that header verbatim -- and Cloud Run Jobs sends an unhandled
    exception's traceback straight to Cloud Logging, a durable, exportable
    sink. That happened once during this batch's own deploy testing (a
    scratch, since-flagged-for-rotation token; see README/report). Every
    call site that can put the token in an argv list goes through this
    function instead of a bare `subprocess.run`.
    """
    try:
        subprocess.run(args, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise subprocess.CalledProcessError(
            exc.returncode,
            [_redact(arg, token) for arg in exc.cmd],
            output=_redact(exc.output or "", token),
            stderr=_redact(exc.stderr or "", token),
        ) from None


def _wait_until_serving(url: str, timeout: float = 10.0) -> None:
    """Block until `url` answers or `timeout` elapses.

    The scratch HTTP server (below) is started with subprocess.Popen, which
    returns before the socket is necessarily accepting connections. Without
    this, the baseline scan can race the server and hit a bare
    ConnectionRefusedError -- observed locally as a flaky first run.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1).close()
            return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    raise TimeoutError(f"{url} did not start serving within {timeout}s")


def _build_vertex_model(config):
    """A model.generate(prompt, images) adapter over Vertex AI Gemini.

    Imported lazily so this module can be imported (and its pure functions
    tested) without google-genai or live Vertex credentials present.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(vertexai=True, project=config.project_id, location=config.vertex_location)

    class VertexModel:
        def generate(self, prompt: str, images: list[bytes] | None = None) -> str:
            parts = [prompt]
            for image in images or []:
                parts.append(types.Part.from_bytes(data=image, mime_type="image/png"))
            return client.models.generate_content(model=config.model, contents=parts).text

    return VertexModel()


def main() -> None:
    args = json.loads(sys.argv[1])
    config = load_config(prefix="a11y")
    setup_telemetry(config, "a11y-worker")

    run_id = run_id_for(args["repo"], args["pr"])
    store = Store(config, client=FakeFirestore() if os.getenv("USE_FAKE_STORE") else None)
    store.put(
        "runs",
        run_id,
        {"id": run_id, "repo": args["repo"], "pr": args["pr"], "status": "running", "fixed": 0},
    )

    # GITHUB_TOKEN comes from Secret Manager in Cloud Run (--set-secrets) and
    # from `gh auth token`, exported by hand, for a local run -- see README.
    # .strip(): verified live that a Secret Manager value can carry a
    # trailing newline (however it was written into the secret), and a
    # newline inside an HTTP header value breaks `http.extraheader` outright
    # -- git failed with "could not read Username" against a header that
    # *looked* well-formed in the source.
    token = os.environ["GITHUB_TOKEN"].strip()
    auth_header = _git_auth_header(token)
    branch = f"a11y-fixes/{args['head_sha'][:7]}"

    with tempfile.TemporaryDirectory() as workdir:
        # The token is passed via `-c http.extraheader`, not embedded in the
        # remote URL: a URL-embedded token round-trips into `.git/config` and
        # `git remote -v` on a filesystem this job does not own. `-c` scopes
        # it to these two commands. It is still visible to `ps` on this
        # container for the life of the subprocess -- accepted here because a
        # Cloud Run Job's container is single-tenant for its own execution.
        _run_git(
            [
                "git", "clone", "--depth", "1", "--branch", args["head_ref"],
                "-c", f"http.extraheader={auth_header}",
                f"https://github.com/{args['repo']}.git", workdir,
            ],
            token,
        )

        port = "8081"
        serve = subprocess.Popen(
            [sys.executable, "-m", "http.server", port, "--directory", workdir],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            url = f"http://127.0.0.1:{port}/index.html"
            _wait_until_serving(url)
            result = run_remediation(_build_vertex_model(config), store, run_id, workdir, url)
        finally:
            serve.terminate()
            serve.wait(timeout=5)

        reasons = unsafe_reasons(result)
        if reasons:
            log_event(
                "run.unsafe", severity="ERROR", run_id=run_id,
                repo=args["repo"], pr=args["pr"], reasons=reasons,
            )
            store.put("runs", run_id, build_run_record(run_id, args, result, status="unsafe"))
            return

        if result.verified:
            _run_git(["git", "-C", workdir, "checkout", "-b", branch], token)
            # -c user.name / user.email: the container has no ~/.gitconfig and
            # `git clone` does not set one either -- verified live, the commit
            # failed with git's own "Please tell me who you are" (exit 128)
            # before this was added. Scoped to this one command, like the
            # auth header, rather than a global config write this job's
            # container does not own.
            _run_git(
                ["git", "-C", workdir,
                 "-c", "user.name=a11y-agent", "-c", "user.email=a11y-agent@users.noreply.github.com",
                 "commit", "-am",
                 f"fix: {len(result.verified)} verified accessibility violation(s)"],
                token,
            )
            _run_git(
                ["git", "-C", workdir, "-c", f"http.extraheader={auth_header}",
                 "push", "origin", f"HEAD:refs/heads/{branch}"],
                token,
            )

        ref = PullRequestRef(args["repo"], args["pr"], args["head_ref"], args["head_sha"])
        pr_url = open_fix_pr(
            _github_client(token), ref, result.verified, result.triaged,
            screenshots={}, dropped_audit=result.dropped_audit,
        )

    store.put(
        "runs", run_id,
        build_run_record(run_id, args, result, status="complete", pr_url=pr_url),
    )
    log_event(
        "run.complete", run_id=run_id, fixed=len(result.verified),
        triaged=len(result.triaged), pr_url=pr_url,
    )


def _github_client(token: str):
    from github import Github

    return Github(token)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - see the docstring
        # Last-resort scrub. _run_git already redacts the token from a
        # CalledProcessError it raises, but this is defense in depth for
        # anything else that could put the token in a message reaching the
        # default, uncaught-exception traceback -- which Cloud Run Jobs
        # forwards to Cloud Logging verbatim. GITHUB_TOKEN may be unset if
        # main() failed before reading it, hence the getenv rather than []..
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        raise RuntimeError(_redact(f"{type(exc).__name__}: {exc}", token)) from None
