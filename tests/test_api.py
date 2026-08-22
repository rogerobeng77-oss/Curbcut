from fastapi.testclient import TestClient

from app.main import app, store

client = TestClient(app)


def test_lists_runs():
    store.put("runs", "r1", {"repo": "acme/site", "pr": 42, "status": "complete", "fixed": 3})
    response = client.get("/api/runs")
    assert response.status_code == 200
    assert any(run["repo"] == "acme/site" for run in response.json())


def test_returns_a_single_run():
    store.put("runs", "r2", {"repo": "acme/site", "pr": 7, "status": "running", "fixed": 0})
    assert client.get("/api/runs/r2").json()["pr"] == 7


def test_returns_404_for_unknown_run():
    assert client.get("/api/runs/nope").status_code == 404


def test_returns_the_audit_trail_in_order():
    store.append_audit("r3", {"step": "scan", "found": 6})
    store.append_audit("r3", {"step": "verify", "verdict": "resolved"})
    trail = client.get("/api/runs/r3/audit").json()
    assert [entry["step"] for entry in trail] == ["scan", "verify"]


def test_serves_the_console_html():
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_run_record_surfaces_the_new_runresult_fields():
    store.put(
        "runs",
        "r4",
        {
            "repo": "acme/site",
            "pr": 9,
            "status": "complete",
            "fixed": 2,
            "safe_to_ship": False,
            "tree_modified": True,
            "audit_complete": False,
            "reappeared": 1,
            "unreverted": 1,
            "dropped_audit": 2,
        },
    )
    body = client.get("/api/runs/r4").json()
    assert body["safe_to_ship"] is False
    assert body["tree_modified"] is True
    assert body["audit_complete"] is False
    assert body["reappeared"] == 1
    assert body["unreverted"] == 1
    assert body["dropped_audit"] == 2
