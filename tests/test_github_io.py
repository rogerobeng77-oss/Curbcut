from app.github_io import PullRequestRef, open_fix_pr, parse_pr_event
from app.patcher import Patch

PATCH = Patch(
    path="index.html", line=2, old="<img>", new='<img alt="Logo">', rationale="Added alt text."
)
REF = PullRequestRef(repo="acme/site", number=42, head_ref="feature", head_sha="abc123")


def _event(action: str = "opened") -> dict:
    return {
        "action": action,
        "pull_request": {"number": 42, "head": {"ref": "feature", "sha": "abc123"}},
        "repository": {"full_name": "acme/site"},
    }


def test_parses_opened_pull_request():
    assert parse_pr_event(_event("opened")) == REF


def test_parses_synchronize_action():
    assert parse_pr_event(_event("synchronize")) == REF


def test_ignores_closed_action():
    assert parse_pr_event(_event("closed")) is None


def test_ignores_non_pull_request_events():
    assert parse_pr_event({"action": "opened", "issue": {"number": 1}}) is None


class FakeRepo:
    def __init__(self):
        self.created = []

    def create_pull(self, title, body, head, base):
        self.created.append({"title": title, "body": body, "head": head, "base": base})
        return type("PR", (), {"html_url": "https://github.com/acme/site/pull/43"})()


class FakeClient:
    def __init__(self):
        self.repo = FakeRepo()

    def get_repo(self, name):
        return self.repo


def test_open_fix_pr_returns_url_and_targets_head_branch():
    client = FakeClient()
    url = open_fix_pr(client, REF, [PATCH], triaged=[], screenshots={})
    assert url == "https://github.com/acme/site/pull/43"
    assert client.repo.created[0]["base"] == "feature"


def test_pr_body_lists_each_fix_with_its_rationale():
    client = FakeClient()
    open_fix_pr(client, REF, [PATCH], triaged=[], screenshots={})
    body = client.repo.created[0]["body"]
    assert "index.html" in body
    assert "Added alt text." in body


def test_pr_body_states_the_automation_limit():
    client = FakeClient()
    open_fix_pr(client, REF, [PATCH], triaged=[], screenshots={})
    body = client.repo.created[0]["body"].lower()
    assert "subset of wcag" in body
    assert "not an overlay" in body


def test_pr_body_lists_triaged_items_for_humans():
    client = FakeClient()
    open_fix_pr(
        client, REF, [PATCH],
        triaged=[{"rule": "color-contrast", "reason": "unresolved"}],
        screenshots={},
    )
    body = client.repo.created[0]["body"]
    assert "color-contrast" in body
    assert "unresolved" in body


def test_no_pr_is_opened_when_there_are_no_verified_patches():
    client = FakeClient()
    assert open_fix_pr(client, REF, [], triaged=[], screenshots={}) is None
    assert client.repo.created == []


def test_pr_body_notes_an_incomplete_audit_trail():
    client = FakeClient()
    open_fix_pr(
        client, REF, [PATCH], triaged=[], screenshots={},
        dropped_audit=[{"step": "verify", "rule": "image-alt"}],
    )
    body = client.repo.created[0]["body"].lower()
    assert "audit trail" in body
    assert "incomplete" in body


def test_pr_body_says_nothing_about_the_audit_trail_when_it_is_complete():
    client = FakeClient()
    open_fix_pr(client, REF, [PATCH], triaged=[], screenshots={}, dropped_audit=[])
    body = client.repo.created[0]["body"].lower()
    assert "incomplete" not in body
