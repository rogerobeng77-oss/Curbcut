import os
from dataclasses import dataclass

DEFAULT_PROJECT = "total-fiber-399801"
# Cloud Run, Firestore, Pub/Sub, and Cloud Run Jobs are regional and live in
# us-central1. Gemini model access via google-genai/Vertex is routed through
# GOOGLE_CLOUD_LOCATION, but the `gemini-3.5-flash` model is only served from
# the `global` Vertex endpoint (regional hosts such as us-central1, us-east5,
# and europe-west4 all 404 for it). These two locations must NOT be
# collapsed back into a single field — infrastructure location and Vertex
# model-access location are genuinely different values.
DEFAULT_LOCATION = "us-central1"
DEFAULT_VERTEX_LOCATION = "global"
DEFAULT_MODEL = "gemini-3.5-flash"


@dataclass(frozen=True)
class Config:
    project_id: str
    location: str
    vertex_location: str
    model: str
    firestore_prefix: str


def load_config(prefix: str) -> Config:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
    # These assignments are deliberately unconditional, not `setdefault`.
    # `adk deploy cloud_run --region us-central1` bakes
    # GOOGLE_CLOUD_LOCATION=us-central1 into the container; with `setdefault`
    # that ambient value would survive, google-genai would route to the
    # regional Vertex host, and `gemini-3.5-flash` would 404 there — silently,
    # at runtime, in production only. `setdefault` would also make
    # GCP_VERTEX_LOCATION unable to rescue it, and leave the returned Config
    # disagreeing with the process env. load_config is the authority on these
    # two variables; it always wins.
    os.environ["GOOGLE_CLOUD_PROJECT"] = os.getenv("GCP_PROJECT", DEFAULT_PROJECT)
    # google-genai reads GOOGLE_CLOUD_LOCATION to route model calls, so this
    # must be the Vertex model-access location, not the infra location.
    os.environ["GOOGLE_CLOUD_LOCATION"] = os.getenv(
        "GCP_VERTEX_LOCATION", DEFAULT_VERTEX_LOCATION
    )
    return Config(
        project_id=os.getenv("GCP_PROJECT", DEFAULT_PROJECT),
        location=os.getenv("GCP_LOCATION", DEFAULT_LOCATION),
        vertex_location=os.getenv("GCP_VERTEX_LOCATION", DEFAULT_VERTEX_LOCATION),
        model=os.getenv("GEMINI_MODEL", DEFAULT_MODEL),
        firestore_prefix=prefix,
    )
