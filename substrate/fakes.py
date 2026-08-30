import copy
import itertools


class FakeModel:
    """Scripted stand-in for a Gemini model. Tests assert on `calls`."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self._index = 0
        self.calls: list[dict] = []

    def generate(
        self,
        prompt: str,
        images: list[bytes] | None = None,
        response_schema: dict | None = None,
    ) -> str:
        # Recorded, not honoured: a double that silently dropped an argument
        # the real client acts on would let a caller stop passing it and keep
        # a green suite.
        self.calls.append(
            {"prompt": prompt, "images": images, "response_schema": response_schema}
        )
        assert self._index < len(self._responses), (
            f"FakeModel exhausted after {self._index} call(s); "
            f"got an unexpected call with prompt={prompt!r}"
        )
        response = self._responses[self._index]
        self._index += 1
        return response


class _Missing:
    """Sentinel for a field the document does not carry at all."""


_MISSING = _Missing()

# Firestore operator semantics. `in` tests membership of the *field value*
# in the caller-supplied sequence, which is the reverse of Python's `in`.
_OPS = {
    "==": lambda actual, expected: actual == expected,
    "in": lambda actual, expected: actual in expected,
    ">=": lambda actual, expected: actual >= expected,
    "<=": lambda actual, expected: actual <= expected,
}


def _normalise_filter(field_path, op_string, value, filter):
    """Accept every shape ``google-cloud-firestore`` accepts.

    - ``where("status", "==", "queued")`` -- the legacy positional form, still
      accepted by the real client.
    - ``where(filter=FieldFilter("status", "==", "queued"))`` -- the current
      form. Duck-typed via ``field_path``/``op_string``/``value`` so the fake
      needs no dependency on ``google.cloud.firestore``.
    - ``where(filter=("status", "==", "queued"))`` -- a plain tuple, which the
      real client does not take but is convenient in tests.
    """
    if filter is None and isinstance(field_path, (tuple, list)) and op_string is None:
        filter = field_path
        field_path = None

    if filter is not None:
        if field_path is not None or op_string is not None or value is not None:
            raise TypeError("pass either filter= or the positional field/op/value triple")
        if isinstance(filter, (tuple, list)):
            if len(filter) != 3:
                raise TypeError(f"filter tuple must be (field_path, op_string, value), got {filter!r}")
            field_path, op_string, value = filter
        else:
            try:
                field_path = filter.field_path
                op_string = filter.op_string
                value = filter.value
            except AttributeError as exc:
                raise TypeError(
                    f"unsupported filter {filter!r}: expected a 3-tuple or an object with "
                    "field_path/op_string/value (e.g. google.cloud.firestore_v1 FieldFilter)"
                ) from exc
    elif field_path is None or op_string is None:
        raise TypeError("where() needs field_path and op_string, or filter=")

    if op_string not in _OPS:
        raise ValueError(f"FakeCollection.where does not support {op_string!r}; supported: {sorted(_OPS)}")
    return (field_path, op_string, value)


def _matches(data, spec) -> bool:
    field_path, op_string, expected = spec
    actual = data.get(field_path, _MISSING)
    if actual is _MISSING:
        # Firestore never returns documents missing the filtered field.
        return False
    return _OPS[op_string](actual, expected)


class FakeSnapshot:
    def __init__(self, data, doc_id):
        self._data = data
        self.id = doc_id

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        # Copy out, so a caller mutating the returned dict cannot reach back
        # into the store and change what a later read sees.
        return copy.deepcopy(self._data) if self._data is not None else None


class FakeDoc:
    def __init__(self, client, path):
        self._client, self._path = client, path

    def set(self, data) -> None:
        # Copy in, so a test that reuses one payload dict across writes does
        # not retroactively rewrite documents it already stored.
        self._client.data[self._path] = copy.deepcopy(data)
        self._client.bump_version(self._path)

    def get(self, transaction=None) -> FakeSnapshot:
        # `transaction=` mirrors the real DocumentReference.get. Recording the
        # version read is what lets the commit detect that someone else wrote
        # the document in between.
        if transaction is not None:
            transaction.record_read(self._path, self._client.version(self._path))
        return FakeSnapshot(self._client.data.get(self._path), self._path.split("/")[-1])


class FakeCollection:
    def __init__(self, client, name, filters=()):
        self._client, self._name = client, name
        self._filters = tuple(filters)

    def document(self, doc_id) -> FakeDoc:
        return FakeDoc(self._client, f"{self._name}/{doc_id}")

    def where(self, field_path=None, op_string=None, value=None, *, filter=None):
        """Mirrors the real client's signature; returns a filtered view.

        Returning a new view rather than ``self`` is what makes the filter
        observable: chained ``where()`` calls compose, and the unfiltered
        collection keeps returning everything.
        """
        spec = _normalise_filter(field_path, op_string, value, filter)
        return FakeCollection(self._client, self._name, self._filters + (spec,))

    def stream(self):
        prefix = f"{self._name}/"
        return [
            FakeSnapshot(value, key[len(prefix):])
            for key, value in self._client.data.items()
            if key.startswith(prefix) and all(_matches(value, spec) for spec in self._filters)
        ]


_TRANSACTION_IDS = itertools.count(1)


class FakeTransaction:
    """The smallest thing ``@firestore.transactional`` can drive.

    The real decorator (``google.cloud.firestore_v1.transaction._Transactional``)
    calls ``_clean_up()``, ``_begin(retry_id=...)``, the wrapped function and
    then ``_commit()``; it reads ``_read_only`` and ``_max_attempts``, retries
    the whole thing on ``google.api_core.exceptions.Aborted``, and calls
    ``_rollback()`` on any error. That underscored surface is what this class
    implements, plus ``set()`` and the read-versioning behind it.

    This is deliberately NOT a transaction emulator. Reads outside the recorded
    read set are not isolated, buffered writes are invisible to reads in the
    same transaction, and there is no read-only mode, no ``get_all``, no query
    support. All it does is make a lost update abort instead of overwriting.
    """

    def __init__(self, client, max_attempts=5):
        self._client = client
        self._max_attempts = max_attempts
        # The decorator reads this to decide whether Aborted is retryable.
        # This shim is write-capable only, so it is always False.
        self._read_only = False
        self._clean_up()

    def _clean_up(self) -> None:
        self._id = None
        self._reads: dict[str, int] = {}
        self._writes: list[tuple] = []

    def _begin(self, retry_id=None) -> None:
        self._id = retry_id or f"fake-txn-{next(_TRANSACTION_IDS)}"

    def _rollback(self) -> None:
        self._clean_up()

    def _commit(self) -> list:
        # Imported here, not at module scope, so the rest of the fake stays
        # free of google.cloud dependencies (see _normalise_filter). It has to
        # be the real Aborted class: that exact type is what the decorator
        # catches to trigger a retry.
        from google.api_core import exceptions

        for path, version in self._reads.items():
            if self._client.version(path) != version:
                raise exceptions.Aborted(
                    f"document {path} was written after this transaction read it"
                )
        for doc, data in self._writes:
            doc.set(data)
        self._clean_up()
        return []

    def record_read(self, path, version) -> None:
        # First read wins: that is the snapshot the caller computed from, so
        # that is the version the commit has to still find in place.
        self._reads.setdefault(path, version)

    def set(self, doc, data) -> None:
        self._writes.append((doc, copy.deepcopy(data)))


class FakeFirestore:
    """In-memory stand-in for a Firestore client. `data` maps "collection/doc" to dicts."""

    def __init__(self):
        self.data: dict[str, dict] = {}
        # Per-document write counter, used only for transaction conflicts.
        self._versions: dict[str, int] = {}

    def collection(self, name) -> FakeCollection:
        return FakeCollection(self, name)

    def version(self, path) -> int:
        return self._versions.get(path, 0)

    def bump_version(self, path) -> None:
        self._versions[path] = self.version(path) + 1

    def transaction(self, max_attempts=5) -> FakeTransaction:
        return FakeTransaction(self, max_attempts)
