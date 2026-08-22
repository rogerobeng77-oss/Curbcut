from app.runner import RunResult
from job.worker import build_run_record, run_id_for, unsafe_reasons


def _result(**overrides) -> RunResult:
    result = RunResult()
    result.final_scan_ok = True
    for key, value in overrides.items():
        setattr(result, key, value)
    return result


def test_a_safe_run_has_no_unsafe_reasons():
    assert unsafe_reasons(_result()) == []


def test_final_scan_that_never_ran_is_unsafe():
    reasons = unsafe_reasons(_result(final_scan_ok=False))
    assert reasons
    assert "final" in reasons[0]


def test_a_reappeared_violation_is_unsafe():
    reasons = unsafe_reasons(_result(reappeared=[{"rule": "image-alt", "selector": ".logo"}]))
    assert any("reappeared" in reason for reason in reasons)


def test_an_unreverted_patch_is_unsafe():
    reasons = unsafe_reasons(
        _result(unreverted=[{"rule": "image-alt", "path": "index.html", "line": 2, "new": "x"}])
    )
    assert any("reverted" in reason for reason in reasons)


def test_unsafe_reasons_is_empty_exactly_when_safe_to_ship_is_true():
    for result in (
        _result(),
        _result(final_scan_ok=False),
        _result(reappeared=[{"rule": "x", "selector": "y"}]),
        _result(unreverted=[{"rule": "x", "path": "p", "line": 1, "new": "n"}]),
    ):
        assert (not unsafe_reasons(result)) == result.safe_to_ship


def test_run_id_joins_repo_and_pr_number():
    assert run_id_for("acme/site", 42) == "acme_site-42"


def test_run_record_carries_the_runresult_fields_the_console_needs():
    result = _result()
    result.verified = ["patch-1", "patch-2"]
    record = build_run_record(
        "acme_site-42", {"repo": "acme/site", "pr": 42}, result, status="complete", pr_url="https://x"
    )
    assert record == {
        "id": "acme_site-42",
        "repo": "acme/site",
        "pr": 42,
        "status": "complete",
        "fixed": 2,
        "pr_url": "https://x",
        "safe_to_ship": True,
        "tree_modified": False,
        "audit_complete": True,
        "reappeared": 0,
        "unreverted": 0,
        "dropped_audit": 0,
        "triaged": 0,
    }


def test_run_record_reports_an_unsafe_run_honestly():
    result = _result(
        final_scan_ok=True,
        unreverted=[{"rule": "image-alt", "path": "index.html", "line": 2, "new": "x"}],
        dropped_audit=[{"step": "verify"}],
    )
    record = build_run_record("id", {"repo": "acme/site", "pr": 1}, result, status="unsafe")
    assert record["safe_to_ship"] is False
    assert record["tree_modified"] is True
    assert record["audit_complete"] is False
    assert record["dropped_audit"] == 1
    assert record["pr_url"] is None
