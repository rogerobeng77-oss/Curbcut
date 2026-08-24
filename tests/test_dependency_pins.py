"""Guards against dependency upgrades that break at runtime but not at import.

The Dockerfile installs from `pyproject.toml`, not from a lock file, so every
container rebuild re-resolves the dependency tree against the version floors.
That is fine until a transitive upgrade changes behaviour rather than API --
which is invisible to every other test in this suite, because the fakes never
exercise the real client.
"""

from google.auth.credentials import AnonymousCredentials
from google.cloud import firestore


def test_default_database_is_not_percent_encoded():
    """google-api-core 2.35.0 encoded "(default)" as "%28default%29".

    The failure surfaced only in production, as `400 Invalid database id
    %28default%29` on every query, because the offending path is built from
    library internals rather than from anything this codebase passes in.
    """
    client = firestore.Client(project="demo-proj", credentials=AnonymousCredentials())

    assert client._database_string == "projects/demo-proj/databases/(default)"
    assert "%" not in client._database_string
