"""Every model implementation must accept the same generate() call.

The remediation loop calls `model.generate(...)` against whichever object it
was handed: a FakeModel in tests, the real Vertex adapter in production. When
those two drift, no test fails -- tests never build the production one -- and
the failure surfaces as a run that patches nothing.

That is not hypothetical. `job/worker.py` carried its own second Vertex
adapter. `response_schema` was added to the substrate adapter, the fake, and
a local test double; the worker's copy was missed. The next real run triaged
all seven violations with `unexpected keyword argument 'response_schema'`.
"""

import inspect

from substrate.fakes import FakeModel
from substrate.gemini import GeminiModel


def _params(func):
    return [p for p in inspect.signature(func).parameters if p != "self"]


def test_fake_and_real_models_accept_the_same_arguments():
    assert _params(FakeModel.generate) == _params(GeminiModel.generate)


def test_generate_accepts_response_schema():
    """JSON mode is the reason the signature grew; keep it load-bearing."""
    for impl in (FakeModel, GeminiModel):
        assert "response_schema" in _params(impl.generate), impl


def test_worker_builds_the_substrate_adapter_not_a_second_one():
    """The worker must not reintroduce a parallel implementation."""
    from substrate.config import load_config
    from job.worker import _build_vertex_model

    model = _build_vertex_model(load_config(prefix="a11y"))

    assert isinstance(model, GeminiModel)
    assert _params(type(model).generate) == _params(FakeModel.generate)
