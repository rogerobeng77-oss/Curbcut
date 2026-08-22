import os

# app.main builds a module-level Store at import time. Setting this before
# any test module imports app.main makes that Store a FakeFirestore instead
# of one that opens a real connection during collection.
os.environ["USE_FAKE_STORE"] = "1"

import pytest


@pytest.fixture(autouse=True)
def restore_environ():
    """Snapshot and restore ``os.environ`` around every test.

    ``load_config`` mutates the process environment (GOOGLE_GENAI_USE_VERTEXAI,
    GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION) by design. Without this
    fixture those writes escape the test that made them and persist for the
    rest of the pytest session, so a test run in isolation and the same test
    run inside the full suite see different starting environments — and any
    later test that boots the app in-process observes whichever value the
    first ``load_config`` caller happened to write. This lives in conftest so
    every test module in the substrate inherits it.
    """
    snapshot = os.environ.copy()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)
