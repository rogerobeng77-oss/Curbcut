import json
import sys
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Status, StatusCode

from substrate.config import Config

_tracer = None


def setup_telemetry(config: Config, service_name: str, span_processor=None) -> None:
    """Install a tracer provider. Pass `span_processor` in tests; omit it in
    production to export to Cloud Trace.

    OpenTelemetry allows the global tracer provider to be set only once per
    process (`trace.set_tracer_provider` warns and no-ops on later calls), so
    a test suite that calls this more than once — as this one does — cannot
    rely on `trace.get_tracer(...)` picking up the provider built here after
    the first call. Instead we get the tracer directly off the local
    `provider` object, which always has the span processor just attached to
    it, regardless of whether the global set succeeded.
    """
    global _tracer
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if span_processor is None:
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        span_processor = BatchSpanProcessor(CloudTraceSpanExporter(project_id=config.project_id))
    provider.add_span_processor(span_processor)
    trace.set_tracer_provider(provider)
    _tracer = provider.get_tracer(service_name)


@contextmanager
def span(name: str, **attributes):
    tracer = _tracer or trace.get_tracer("substrate")
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, value)
        try:
            yield current
        except Exception as exc:
            current.set_status(Status(StatusCode.ERROR, str(exc)))
            current.record_exception(exc)
            raise


def log_event(event: str, severity: str = "INFO", **fields) -> None:
    """Structured JSON to stdout — Cloud Logging parses this natively."""
    print(json.dumps({"event": event, "severity": severity, **fields}), file=sys.stdout, flush=True)
