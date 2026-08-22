import datetime
import logging

from substrate.config import Config
from substrate.guards import redact_pii

_log = logging.getLogger(__name__)

# The audit trail is the reasoning chain all three demos render, so it is kept
# as one document per run holding an ordered `entries` list, each entry stamped
# with a client-assigned `seq`. One read returns the whole trail already
# ordered: no server timestamps to tie-break, no composite index to keep, and
# no way for a display to show entries out of order.
#
# What that shape costs, and what it does not:
#
# - Every append rewrites the whole trail, so N entries cost O(N^2) bytes
#   written. Fine for a demo run of tens of entries; not a log sink.
# - Firestore caps a document at 1 MiB (1,048,576 bytes), so a run's trail has
#   a hard ceiling. A run that could exceed it needs per-entry documents.
# - A single document is the smallest unit Firestore can split across servers,
#   so sustained concurrent writes to one trail show up as contention and
#   latency. (Firestore's current guidance states this as hotspotting, not as a
#   fixed per-second quota -- there is no published per-document write-rate
#   number to design against.)
# - Concurrent appends do not lose entries silently: append_audit reads and
#   writes inside a transaction, so a losing writer aborts and re-runs instead
#   of overwriting. That holds up to the retry ceiling. Past it the entry is
#   not written at all, loudly: google.cloud.firestore_v1 retries an aborted
#   transaction transaction._max_attempts times (defaulting to
#   base_transaction.MAX_ATTEMPTS = 5 in google-cloud-firestore 2.28.1, the
#   version in uv.lock), then rolls back and raises
#   ValueError("Failed to commit transaction in 5 attempts.") chained from the
#   last Aborted. append_audit lets that propagate and no caller in this repo
#   catches it -- deliberate for a demo, where a dropped audit entry should
#   stop the run rather than leave a trail with an invisible hole.


def _redact(value):
    """Redact PII everywhere in a value, not only in top-level strings.

    Audit entries are free-form dicts written by agent code across three
    projects: findings arrive in lists, tool results in nested dicts. redact_pii
    is the only barrier before the entry is persisted, so it has to reach the
    leaves -- a top-level-strings-only pass lets the shape an agent naturally
    produces walk straight past it.

    Bytes are decoded as UTF-8 and redacted when that succeeds. When it fails
    the content is dropped for a length marker: a regex cannot scan a binary
    blob (a scanned form, a screenshot) for the PII inside it, and storing
    unscanned bytes would put data past the barrier that this function is.

    Anything this function does not recognise passes through unscanned. That
    fall-through has leaked twice (set, then DocumentReference), both times
    found by a reviewer rather than by anything the code said, so it now warns
    for every type it does not recognise beyond the scalars listed at the
    bottom. It still does not raise: a leak is bad, an audit write that kills
    the run is worse.
    """
    if isinstance(value, str):
        return redact_pii(value)
    # Checked before dict/list/tuple/set because that is the encoder's own
    # precedence: firestore_v1/_helpers.encode_value tests
    # ``getattr(value, "_document_path", None)`` *before* its list and dict
    # branches, so anything carrying that attribute is stored as a
    # reference_value no matter what else it is. Verified against
    # google-cloud-firestore 2.28.1.
    document_path = _document_path_of(value)
    if isinstance(document_path, str):
        # Returned as a redacted path string, not as a rebuilt reference.
        # Firestore document IDs permit "@", so the PII is usually the ID
        # segment -- a real reference to patients/sam@example.com persists that
        # address in full. Rebuilding a reference around the redacted path
        # would need the DocumentReference class imported here (substrate keeps
        # google.cloud out of its import path) and would produce a live,
        # writable address for a document that cannot exist: anything that
        # follows the trail would miss on read and create junk on write. A
        # reference_value carries no referential integrity to preserve -- it is
        # a path string with a type tag, and the tag is worth nothing once the
        # path is no longer real. Changing type here also matches what _redact
        # already does for sets (-> list) and undecodable bytes (-> marker).
        return redact_pii(document_path)
    if isinstance(value, dict):
        return _redact_mapping(value)
    # list/tuple handled separately, rather than via type(value)(...), so that
    # subclasses and namedtuples cannot blow up on reconstruction.
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    if isinstance(value, (set, frozenset)):
        # Returned as a list, not a set. Firestore encodes set and frozenset as
        # an ArrayValue (firestore_v1/_helpers.py encode_value branches on
        # list/tuple/set/frozenset together), so a set never round-trips as a
        # set anyway -- and redaction can map two distinct members onto the
        # same string, which a set would silently deduplicate away. A list
        # keeps the member count the caller wrote.
        return [_redact(item) for item in value]
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            return f"[BINARY:{len(value)}B]"
        return redact_pii(text).encode("utf-8")
    # Deliberate silent pass-throughs: Firestore stores each of these as a
    # scalar with no string inside for redact_pii to scan. bool is an int
    # subclass and DatetimeWithNanoseconds a datetime subclass, so both are
    # already covered. (An int can still carry PII -- 4155550132 persists as an
    # integer_value -- but redact_pii is str-only by signature; widening it is
    # a contract change, tracked separately, not a reason to make every integer
    # in every audit entry log a warning.)
    if isinstance(value, (type(None), int, float, datetime.datetime)):
        return value
    _warn_unhandled(value)
    return value


def _document_path_of(value):
    """The value's Firestore document path, or None if it has none.

    ``_document_path`` is a property that raises ValueError when the reference
    was built without a client, and getattr's default does not swallow that.
    Declining to redact such a reference costs nothing: encode_value reads the
    same property and raises the same ValueError, so it was never going to
    reach storage. Catching it only keeps _redact from being what raises.
    """
    try:
        return getattr(value, "_document_path", None)
    except ValueError:
        return None


def _warn_unhandled(value) -> None:
    """Leave a trace when a type reaches storage unscanned.

    Firestore encodes more types than _redact recognises (GeoPoint, Vector,
    and whatever a future client version adds), and every one of those persists
    without ever being looked at for PII. The types Firestore *cannot* encode
    raise TypeError in encode_value before anything is written, so they are not
    exposures -- but they land here too, and a warning naming them is cheaper
    than a branch guessing which is which.

    TODO: emit this through substrate.telemetry.log_event once that module
    exists; it does not at this commit, and google-cloud-logging is not a
    dependency, so this is the plainest thing available.
    """
    cls = type(value)
    _log.warning(
        "_redact passed through an unhandled type: %s.%s -- if Firestore can "
        "encode it, it was persisted without being scanned for PII",
        cls.__module__,
        cls.__qualname__,
    )


def _redact_mapping(mapping: dict) -> dict:
    redacted: dict = {}
    for key, value in mapping.items():
        if isinstance(key, bytes):
            # protobuf accepts a bytes key where a MapValue wants a string
            # field name, so a bytes key really does persist -- and it persists
            # as a string. (int and tuple keys raise TypeError there, so those
            # cannot reach storage at all.) Redact it through the same bytes
            # path as values, then carry it as the string it will be stored
            # as, so the collision suffixing below applies to it too.
            key = _redact(key)
            if isinstance(key, bytes):
                key = key.decode("utf-8")
        # Keys are redacted too -- agent code can key a dict by an email
        # address or a case number.
        new_key = redact_pii(key) if isinstance(key, str) else key
        # Two different keys can redact to the same string ("[EMAIL]"), which
        # would silently drop one of the values. Suffix instead: this fix is
        # about stopping silent loss, not trading one kind for another.
        if isinstance(new_key, str) and new_key in redacted:
            suffix = 2
            while f"{new_key}#{suffix}" in redacted:
                suffix += 1
            new_key = f"{new_key}#{suffix}"
        redacted[new_key] = _redact(value)
    return redacted


class Store:
    def __init__(self, config: Config, client=None):
        self._prefix = config.firestore_prefix
        if client is None:
            from google.cloud import firestore

            client = firestore.Client(project=config.project_id)
        self._client = client

    def _name(self, collection: str) -> str:
        return f"{self._prefix}_{collection}"

    def put(self, collection: str, doc_id: str, data: dict) -> None:
        self._client.collection(self._name(collection)).document(doc_id).set(data)

    def get(self, collection: str, doc_id: str) -> dict | None:
        snapshot = self._client.collection(self._name(collection)).document(doc_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def query(self, collection: str, field: str, op: str, value) -> list[dict]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        collection_ref = self._client.collection(self._name(collection))
        results = collection_ref.where(filter=FieldFilter(field, op, value)).stream()
        return [doc.to_dict() for doc in results]

    def append_audit(self, run_id: str, entry: dict) -> None:
        # Deferred like the imports above, so importing substrate.store needs
        # no GCP credentials.
        from google.cloud import firestore

        doc_ref = self._client.collection(self._name("audit")).document(run_id)
        redacted = _redact(entry)

        @firestore.transactional
        def append(transaction) -> None:
            # The read and the write must be one transaction. A plain
            # get-then-set loses entries: two writers that read the same
            # snapshot both compute seq = len(entries), and the second set() --
            # a whole-document replace, not a merge -- drops the first writer's
            # entry. Worse, seq stays contiguous afterwards, so the trail looks
            # well-formed and nothing downstream can tell an entry vanished.
            # Inside a transaction the second commit aborts instead, and the
            # decorator re-runs this function against the fresh trail (up to
            # transaction._max_attempts, 5 by default).
            snapshot = doc_ref.get(transaction=transaction)
            record = snapshot.to_dict() if snapshot.exists else {}
            entries = list(record.get("entries") or [])
            # seq is stamped here -- after redaction, inside the transaction --
            # so a caller-supplied "seq" cannot forge the counter, and a retry
            # renumbers against the trail it just re-read.
            entries.append(redacted | {"seq": len(entries)})
            record["entries"] = entries
            transaction.set(doc_ref, record)

        append(self._client.transaction())

    def audit_trail(self, run_id: str) -> list[dict]:
        record = self.get("audit", run_id)
        # .get("entries", []): put("audit", ...) is public, so an audit document
        # without an entries list is reachable, and this is the read path every
        # demo renders -- it must not raise.
        return record.get("entries", []) if record else []
