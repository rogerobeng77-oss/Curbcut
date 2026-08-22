"""Smoke test for the vendored substrate copy.

A vendored package that silently fails to import is the most expensive
failure mode here: it would surface halfway through the next batch instead
of now. This asserts each module imports, that config defaults are the
values the rest of the plan depends on, and that a Store round-trips a
write/read against a FakeFirestore.
"""
from substrate import config, events, fakes, gemini, guards, store, telemetry, web
from substrate.config import load_config
from substrate.fakes import FakeFirestore
from substrate.store import Store


def test_all_eight_substrate_modules_import():
    for module in (config, events, fakes, gemini, guards, store, telemetry, web):
        assert module is not None


def test_load_config_defaults():
    cfg = load_config(prefix="a11y")
    assert cfg.firestore_prefix == "a11y"
    assert cfg.vertex_location == "global"
    assert cfg.location == "us-central1"


def test_store_roundtrips_against_fake_firestore():
    cfg = load_config(prefix="a11y")
    fake = FakeFirestore()
    s = Store(cfg, client=fake)
    s.put("scans", "run-1", {"violations": 6})
    assert s.get("scans", "run-1") == {"violations": 6}
