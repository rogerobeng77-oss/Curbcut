import os

from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult

from app.github_io import parse_pr_event
from substrate.config import load_config
from substrate.events import enqueue_job
from substrate.fakes import FakeFirestore
from substrate.store import Store
from substrate.telemetry import log_event, setup_telemetry
from substrate.web import create_app


class _NullSpanExporter:
    """Discards spans instead of sending them anywhere.

    ``substrate.telemetry.setup_telemetry`` with no ``span_processor`` builds
    a real ``CloudTraceSpanExporter``, which calls ``google.auth.default()``
    at *construction* time — before a single span is ever exported. That
    needs Application Default Credentials, which exist on a Cloud Run
    instance's metadata server but not on a local machine or in the test
    suite (verified: importing this module with no ADC configured raised
    ``google.auth.exceptions.DefaultCredentialsError`` from inside
    ``setup_telemetry``, at collection time, before any test ran).

    ``USE_FAKE_STORE`` is already this project's signal for "no live GCP
    credentials assumed" (set by tests/conftest.py and meant for local runs
    too), so it is reused here rather than adding a second on/off switch for
    the same condition. Cloud Run deploys never set it, so production keeps
    exporting to real Cloud Trace.
    """

    def export(self, spans):
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


config = load_config(prefix="a11y")
setup_telemetry(
    config,
    "a11y-agent",
    span_processor=SimpleSpanProcessor(_NullSpanExporter()) if os.getenv("USE_FAKE_STORE") else None,
)


def handle(payload: dict) -> None:
    ref = parse_pr_event(payload)
    if ref is None:
        log_event("event.ignored", reason="not_a_handled_pr_action")
        return
    execution = enqueue_job(
        config,
        "a11y-worker",
        {"repo": ref.repo, "pr": ref.number, "head_ref": ref.head_ref, "head_sha": ref.head_sha},
    )
    log_event("job.enqueued", repo=ref.repo, pr=ref.number, execution=execution)


app = create_app(on_event=handle, service_name="a11y-agent")

# USE_FAKE_STORE keeps the test suite (and any local run without GCP
# credentials) from opening a real Firestore connection at import time. Never
# set in Cloud Run: production always talks to the real client.
store = Store(config, client=FakeFirestore() if os.getenv("USE_FAKE_STORE") else None)


@app.get("/api/runs")
def list_runs():
    return store.query("runs", "status", "in", ["running", "complete", "failed"])


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = store.get("runs", run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.get("/api/runs/{run_id}/audit")
def get_audit(run_id: str):
    return store.audit_trail(run_id)


@app.get("/")
def console():
    return FileResponse("web/index.html")


app.mount("/static", StaticFiles(directory="web"), name="static")
