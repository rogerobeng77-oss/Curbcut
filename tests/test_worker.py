import subprocess

import pytest

from app.runner import RunResult
from job.worker import _redact, _run_git, build_run_record, run_id_for, unsafe_reasons


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


def test_redact_replaces_every_occurrence_of_every_secret():
    text = _redact("token=sekrit-one and blob=sekrit-two, sekrit-one again", "sekrit-one", "sekrit-two")
    assert "sekrit-one" not in text
    assert "sekrit-two" not in text
    assert text.count("[REDACTED]") == 3


def test_redact_is_a_noop_with_no_secrets():
    assert _redact("nothing to hide") == "nothing to hide"
    assert _redact("nothing to hide", "") == "nothing to hide"


def test_run_git_scrubs_every_secret_from_a_failed_command():
    # Verified live: an unhandled CalledProcessError from a git subprocess
    # invoked with the token in argv (http.extraheader) printed the token in
    # plain text to Cloud Logging via its own .cmd repr. This pins that a
    # failure from _run_git can never carry any passed secret in any field.
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _run_git(
            ["python3", "-c", "import sys; sys.exit(1)", "--token=sekrit-value", "--other=base64blob"],
            "sekrit-value", "base64blob",
        )
    exc = excinfo.value
    assert "sekrit-value" not in str(exc)
    assert "base64blob" not in str(exc)
    assert "sekrit-value" not in " ".join(exc.cmd)
    assert "base64blob" not in " ".join(exc.cmd)


def test_run_git_scrubs_a_derived_secret_a_raw_token_redaction_alone_would_miss():
    # Regression: redacting only the raw token missed the base64-encoded
    # Authorization header this job actually puts in argv (_git_auth_header)
    # -- a different string that still decodes straight back to the token.
    # Verified live: this leaked to Cloud Logging in plain (if
    # base64-"encoded") text before _run_git took *secrets instead of one.
    from job.worker import _git_auth_header

    header = _git_auth_header("sekrit-value")
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _run_git(
            ["python3", "-c", "import sys; sys.exit(1)", f"-c http.extraheader={header}"],
            "sekrit-value", header,
        )
    assert header not in " ".join(excinfo.value.cmd)


def test_run_git_succeeds_silently_for_a_zero_exit():
    _run_git(["python3", "-c", "pass"], "sekrit-value")


def test_git_auth_header_is_basic_auth_with_x_access_token_username():
    from job.worker import _git_auth_header

    assert _git_auth_header("sekrit-value") == (
        "Authorization: Basic eC1hY2Nlc3MtdG9rZW46c2Vrcml0LXZhbHVl"
    )
