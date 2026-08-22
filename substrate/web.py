import inspect
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from starlette.concurrency import run_in_threadpool

from substrate.events import InvalidPushError, parse_pubsub_push
from substrate.guards import redact_pii
from substrate.telemetry import log_event

EventHandler = Callable[[dict], Awaitable[None] | None]

# How much of an exception detail reaches the log line, and the regexes.
#
# The second of two defences against a body that is expensive to redact. The
# first is in `substrate.guards`, where `_EMAIL`'s repeats are bounded so the
# pattern is linear rather than quadratic; that is the real fix and it also
# covers `substrate.store`, which redacts every audit write. This cap is here
# because the first defence is a regex, and a regex is exactly the kind of
# thing a later edit loosens without noticing -- an unbounded repeat put back
# would be quadratic again, and this endpoint is the wire-reachable end of it.
# A constant bound on the input holds whatever the pattern does.
#
# 2000 characters because the value is a diagnostic, not a record: the log
# lines this module emits carry a type name and a message, and every message
# from a type this module actually expects (JSONDecodeError, UnicodeDecodeError,
# binascii.Error, InvalidPushError) is well under it. What gets truncated is a
# handler exception that interpolated a payload -- which is the case the
# redaction exists for, and which nobody wants in full in Cloud Logging either.
_MAX_DETAIL_CHARS = 2000


def _describe(exc: BaseException) -> str:
    """Render an exception for a log line: named, and scrubbed of PII.

    Named because `str(exc)` alone is empty for several of the types that
    reach here -- `str(ClientDisconnect())` is `""`, and an event named for
    its distinguishability that logs `"error": ""` distinguishes nothing.
    The type name is the whole diagnostic in that case.

    Scrubbed because Cloud Logging is durable and exportable, while the same
    payload is redacted on its way into Firestore by `substrate.store`. An
    exception message is an easy place for the payload to walk around that
    barrier: `raise ValueError(f"could not process {payload}")` inside a
    handler is a natural thing to write and puts the whole message body in
    the log. Redaction here is not a substitute for handlers being careful,
    it is the barrier for when they are not.

    Every log site in this module goes through this function rather than only
    the handler one. The messages the *known* types produce carry positions
    and type names, never body content -- verified for `JSONDecodeError`,
    `UnicodeDecodeError` (its buffer appears in `repr`, not `str`),
    `binascii.Error` and every `InvalidPushError` this module's parser
    raises. But two of the four sites are broad `except Exception` arms whose
    exception type is by definition unknown, so "this site is safe" is not a
    property that survives the next change. Uniform redaction removes the
    question, and it leaves the diagnostics intact: the patterns match 9- and
    10-digit runs and email shapes, and a JSON error's "line 1 column 76
    (char 75)" contains neither.

    An earlier version of this docstring justified that as costing "one regex
    pass over a short string". Both halves were wrong, and together they were
    a denial of service. The string is not short -- a handler that
    interpolates the payload puts the whole Pub/Sub body in it, up to 10 MB --
    and the pass was not cheap, because `_EMAIL` was quadratic: measured
    through this endpoint, 3.6 KB took 0.018 s and each doubling multiplied by
    four, reaching 1.674 s at 56 KB. On the event loop, inside the arm that
    exists to keep this endpoint returning 204, that is how the request
    becomes a non-2xx and the message becomes an infinite redelivery.

    So the input is capped at `_MAX_DETAIL_CHARS` before redaction, and the
    pattern itself was bounded in `substrate.guards` (the load-bearing fix,
    since `substrate.store` redacts on the audit path too). Capping before
    rather than after is what bounds the work. It has one cost worth stating:
    an identifier straddling the cut is redacted only in part, so a partial
    value can survive -- measured, up to 320 characters (a 64-character local
    part, "@", and a 255-character domain) when the cut lands just past the
    domain and before its closing "."+TLD, which is enough to defeat the
    match entirely (see substrate.guards for why that combination, and not
    others, escapes whole). Redacting first and truncating after would close
    that and reopen the denial of service, which is the worse trade.

    Formatting is itself guarded, because this function runs *inside* the
    arms that make the endpoint fail closed: an exception whose `__str__`
    raises -- a handler's own custom exception class is enough -- made
    `log_event` raise from within the `except`, and that escaped as a 500,
    which is the retry loop the arms exist to prevent. Verified before this
    guard was written: it returned 500. The type name is resolved by
    `_type_name` for the same reason; see there for why the first attempt at
    this guard was not independent of the try it was guarding.
    """
    name = _type_name(exc)
    try:
        detail = f"{name}: {exc}"
    except Exception:  # noqa: BLE001 - an unprintable exception must not break the ack
        detail = f"{name}: <unprintable>"
    if len(detail) > _MAX_DETAIL_CHARS:
        detail = f"{detail[:_MAX_DETAIL_CHARS]}… <truncated>"
    return redact_pii(detail)


def _type_name(exc: BaseException) -> str:
    """The exception's type name, or a literal placeholder. Never raises.

    Split out from `_describe` because the previous fallback was not
    independent of the try it was covering: both arms evaluated
    `type(exc).__name__`, so an exception whose *name* raised took the except
    arm and then raised again from inside it. That escaped `/events` as a 500,
    which is the Pub/Sub retry loop the arms exist to prevent. `__name__` is a
    normal attribute lookup on the metaclass, so a metaclass with a
    `__name__` property that raises is enough -- reproduced at three sites
    before this split, all 500s: name-and-str raising, name-only raising, and
    the same shape via the parser arm.

    The exact-type check on the result matters as much as the try. Returning
    the looked-up object unchecked moves the problem rather than fixing it: a
    `str` subclass with a raising `__format__` would sail through the lookup
    and then blow up in *both* of `_describe`'s f-strings, including the
    fallback one. `type(name) is str` -- not `isinstance` -- admits only a
    plain `str`, which has no user code on any path `_describe` uses.

    `Exception`, not `BaseException`: a `KeyboardInterrupt` raised out of a
    `__name__` property should still terminate the process, matching what
    `create_app` deliberately lets through.
    """
    try:
        name = type(exc).__name__
    except Exception:  # noqa: BLE001 - an unnameable exception must not break the ack
        return "<unnameable>"
    return name if type(name) is str else "<unnameable>"


def create_app(on_event: EventHandler, service_name: str) -> FastAPI:
    """Build the FastAPI app that fronts a project's Pub/Sub push subscription.

    `POST /events` must fail closed to 204: Pub/Sub retries any non-2xx
    response indefinitely, so anything that escapes this handler as an
    exception becomes a message redelivered forever. Every stage between the
    socket and the ack is therefore wrapped, and each stage has its own event
    name so the cause stays visible in the logs:

    - `event.body_undecodable` -- the bytes never became JSON. Empty body,
      truncated body, arbitrary non-JSON bytes, or a client that disconnects
      mid-read (starlette raises `ClientDisconnect`, which is not a
      `ValueError`). `request.json()` is called *inside* the guarded region,
      not before it.
    - `event.malformed` -- decodable JSON that is not a valid push envelope.
      `parse_pubsub_push` raised `InvalidPushError`, which is the documented
      outcome for a poison message; WARNING, no action needed.
    - `event.parse_failed` -- the parser raised something that is *not*
      `InvalidPushError`. `substrate.events` is explicit that its list of
      guarded cases "is what has been found and covered, not a proof that
      nothing else can escape", so this endpoint does not rely on that
      totality: the requirement here is total even where the collaborator's
      guarantee is not. ERROR rather than WARNING -- it means a substrate
      bug, not a bad client, and it should be loud without being a retry
      loop.
    - `event.handler_failed` -- `on_event` raised. Ack anyway; a handler
      failure is not something Pub/Sub redelivery can fix.

    The only things still allowed through are `BaseException` subclasses that
    are not `Exception` -- `KeyboardInterrupt`, `SystemExit`, `GeneratorExit`.
    Letting a deliberate process-exit signal produce a 500 is correct; the
    container is going down either way.

    `on_event` runs via `run_in_threadpool`, not inline. Calling a sync
    handler directly from this `async def` blocks the event loop for its whole
    duration: measured against real uvicorn, a `GET /healthz` issued 0.3 s
    into a 1.5 s handler took 1.224 s -- it waited out the handler. That
    stalls Cloud Run's liveness and startup probes whenever an event is in
    flight and collapses container concurrency to 1 regardless of the
    configured value.

    An `async def` handler is supported too, and is awaited. Before that it
    silently no-op'd: calling it returned a coroutine nobody awaited, the
    message was lost, the request still 204'd, and the only trace was a
    `RuntimeWarning` on stderr. The signature says sync, but "the type hint
    said so" is not a mechanism, and this substrate is vendored into three
    repos. Detection is `inspect.iscoroutinefunction` for the common case
    plus an `inspect.isawaitable` check on the return value for what that
    misses (a callable object with an `async def __call__`, a wrapper that
    returns a coroutine without being one).

    A body can also decode as JSON while containing a lone UTF-16 surrogate
    (e.g. an unpaired `\\ud800` escape) -- `json.loads` accepts it via the
    `surrogatepass` error handler, so `parse_pubsub_push` correctly returns
    it as a valid payload. Encoding that string back out (Firestore, a log
    line, …) can then raise `UnicodeEncodeError` downstream, after this
    layer has already acked the message. That failure mode starts here but
    belongs to `on_event`'s contract, not this one; a handler that wants to
    catch it before committing to anything should encode-check fields it
    persists rather than assume this layer already validated them.

    The generated docs routes are switched off. `/events` is a
    machine-to-machine endpoint and the service is deployed
    `--allow-unauthenticated`, so `/openapi.json`, `/docs` and `/redoc` are
    schema and surface published to anyone who asks. Removing them does not
    affect a console UI mounted on this same app by a project.
    """
    app = FastAPI(
        title=service_name,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    handler_is_async = inspect.iscoroutinefunction(on_event)

    async def dispatch(payload: dict) -> None:
        if handler_is_async:
            await on_event(payload)
            return
        result = await run_in_threadpool(on_event, payload)
        if inspect.isawaitable(result):
            await result

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.post("/events", status_code=204)
    async def events(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001 - ack; malformed/absent body must not retry-loop
            log_event("event.body_undecodable", severity="WARNING", error=_describe(exc))
            return Response(status_code=204)

        try:
            payload = parse_pubsub_push(body)
        except InvalidPushError as exc:
            # Ack poison messages: a non-2xx here makes Pub/Sub retry forever.
            log_event("event.malformed", severity="WARNING", error=_describe(exc))
            return Response(status_code=204)
        except Exception as exc:  # noqa: BLE001 - ack; a parser bug must not retry-loop either
            log_event("event.parse_failed", severity="ERROR", error=_describe(exc))
            return Response(status_code=204)

        try:
            await dispatch(payload)
        except Exception as exc:  # noqa: BLE001 - ack and record; never retry-loop
            log_event("event.handler_failed", severity="ERROR", error=_describe(exc))

        return Response(status_code=204)

    return app
