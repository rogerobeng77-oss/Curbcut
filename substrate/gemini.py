from substrate.config import Config

# The MIME type is sniffed from magic bytes rather than taken as a
# caller-supplied parameter because the public method's signature is fixed by
# substrate.fakes.FakeModel -- it takes `images: list[bytes]`, nothing else --
# so there is no slot for a MIME hint to travel in.
#
# The accept-list below is a deliberate boundary, not a claim about every
# format Gemini can decode. It covers the formats these three projects can
# actually be handed: PNG and WebP (a11y screenshots -- Chrome's "copy image"
# and several Linux screenshot tools emit WebP), and JPEG, HEIC and HEIF
# (navigator phone photos -- HEIC is the iPhone camera default, and a
# share-sheet or Files-app upload delivers the original rather than the JPEG
# that iOS Safari transcodes for a plain <input type="file">).
#
# GIF, BMP, TIFF and AVIF are recognised and then deliberately REJECTED. None
# of the three projects produces them, and Gemini's documented image-input set
# for generateContent does not list them -- so failing at this boundary, with a
# message naming the format, beats sending an unsupported MIME and getting an
# opaque Vertex 400 mid-demo. Recognising them (rather than letting them fall
# through to the generic "unknown bytes" arm) is what buys the useful message,
# and makes the decision reversible by moving one line.
#
# One honest caveat, since it is checkable locally: the installed google-genai
# additionally lists image/gif, image/bmp and image/tiff in
# `_gaos.types.interactions.imagecontent.ImageContentMimeType`. That is the
# separate `interactions` API surface, not `models.generate_content` which this
# adapter calls, so it does not establish that generateContent accepts them --
# but it is the reason this comment says "not listed" rather than "rejected by
# the API".
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

# ISO base media file format: box size at 0:4, "ftyp" at 4:8, major brand at
# 8:12, compatible brands from 16 on. Verified against real files -- all 16
# iPhone camera captures checked carry major brand `heic`, and a libaom AVIF
# still carries `avif`. Brand-to-MIME follows the HEIF brand registry: the
# HEVC-coded brands are image/heic, the generic image brands are image/heif.
_FTYP_OFFSET = 4
_FTYP_MAGIC = b"ftyp"
_BRAND_MIME_TYPES = {
    b"heic": "image/heic",
    b"heix": "image/heic",
    b"heim": "image/heic",
    b"heis": "image/heic",
    b"hevc": "image/heic",
    b"hevx": "image/heic",
    b"hevm": "image/heic",
    b"hevs": "image/heic",
    b"mif1": "image/heif",
    b"msf1": "image/heif",
    b"heif": "image/heif",
}
_REJECTED_BRANDS = {b"avif": "AVIF", b"avis": "AVIF"}

# Prefixes for the formats rejected by name. BMP's "BM" is only two bytes and
# so a weak signature in general, but it is used here purely to pick a better
# error message on a path that rejects either way -- never to accept anything.
_REJECTED_MAGIC = (
    (b"GIF87a", "GIF"),
    (b"GIF89a", "GIF"),
    (b"II*\x00", "TIFF"),
    (b"MM\x00*", "TIFF"),
    (b"BM", "BMP"),
)

_ACCEPTED = "PNG, JPEG, WebP, HEIC, HEIF"

# Milliseconds -- HttpOptions.timeout is documented in the installed library as
# "Timeout for the request in milliseconds" and is divided by 1000 before it
# reaches httpx (google/genai/_api_client.py: get_timeout_in_seconds). Pinned
# by a test so a library that switched to seconds fails loudly rather than
# turning this budget into a 120000-second one.
#
# Two minutes: enough headroom for a thinking model reasoning over several
# images, and well under Cloud Run's 300s request ceiling, so the timeout fires
# and reports a cause rather than the platform killing the request first.
# Without it there is no deadline at all, and a hung socket in the sole Gemini
# path of three services just stops.
DEFAULT_TIMEOUT_MS = 120_000


class GeminiResponseEmpty(RuntimeError):
    """Raised when a Vertex response carries no usable text.

    Two distinct states reach this, both confirmed by constructing the real
    ``types.GenerateContentResponse``:

    * ``.text is None`` -- no text part at all. A safety block, an empty
      candidate list, or (the realistic case for a thinking model) a candidate
      whose only part is a thought with ``finish_reason=MAX_TOKENS``.
    * ``.text == ''`` -- a real text part that is genuinely empty. This is a
      separate state, and it used to sail through a ``is None`` check and hand
      a caller an empty string as if it were an answer.

    ``GeminiModel.generate`` promises ``-> str``; returning ``None`` through
    that annotation would hand every caller a silent type violation, and
    returning ``''`` would hand them a silent content violation. Raising is the
    loud alternative to both.
    """


class UnrecognisedImageType(ValueError):
    """Raised when image bytes are not one of this module's accepted formats."""


def _sniff_mime_type(data: bytes) -> str:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        # `bytes.startswith(str)` would otherwise leak a bare TypeError reading
        # "startswith first arg must be str or a tuple of str, not bytes",
        # which describes the check rather than the caller's mistake.
        raise UnrecognisedImageType(
            f"image data must be bytes, not {type(data).__name__}; "
            "GeminiModel.generate takes raw image bytes -- if this is a base64 "
            "string, base64.b64decode() it first"
        )
    data = bytes(data)

    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg"
    # Both halves are required: "RIFF" alone is any RIFF container (a WAV, an
    # AVI), and the four bytes at 8 are what make it WebP.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[_FTYP_OFFSET : _FTYP_OFFSET + 4] == _FTYP_MAGIC:
        # An ftyp box alone is any ISO-BMFF file, MP4 included; the brand is
        # what distinguishes a HEIF image from a video container.
        brand = data[8:12]
        mime_type = _BRAND_MIME_TYPES.get(brand)
        if mime_type is not None:
            return mime_type
        rejected = _REJECTED_BRANDS.get(brand)
        if rejected is not None:
            raise UnrecognisedImageType(
                f"{rejected} images are not accepted by this adapter "
                f"(ISO-BMFF brand {brand.decode('ascii', 'replace')!r}); "
                f"convert to one of {_ACCEPTED}"
            )
        raise UnrecognisedImageType(
            "unrecognised ISO-BMFF brand "
            f"{brand.decode('ascii', 'replace')!r}; only the HEIC/HEIF image "
            f"brands are accepted here, out of {_ACCEPTED}"
        )
    for magic, name in _REJECTED_MAGIC:
        if data.startswith(magic):
            raise UnrecognisedImageType(
                f"{name} images are not accepted by this adapter; "
                f"convert to one of {_ACCEPTED}"
            )
    raise UnrecognisedImageType(
        f"cannot determine MIME type for image: first bytes are {data[:12].hex()!r}; "
        f"accepted formats are {_ACCEPTED}"
    )


def _finish_reason(response):
    """First candidate's finish_reason, or None if there are no candidates.

    ``GenerateContentResponse.candidates`` is ``Optional[list[Candidate]]`` and
    really is ``None`` or ``[]`` in practice, so both must be guarded before
    indexing. ``getattr`` rather than attribute access for the same reason the
    ``prompt_feedback`` read below uses it: this runs only on the error path,
    and a differently-shaped double in a downstream project's own tests must
    not turn a useful error into an AttributeError that hides it.
    """
    candidates = getattr(response, "candidates", None)
    if not candidates:
        return None
    return getattr(candidates[0], "finish_reason", None)


class GeminiModel:
    """Vertex-backed Gemini port.

    Interface matches ``substrate.fakes.FakeModel`` exactly -- ``FakeModel``
    is this class's test double, and all three downstream projects code
    against that shape, not against this one.
    """

    def __init__(self, config: Config, client=None, timeout_ms: int = DEFAULT_TIMEOUT_MS):
        self._model = config.model
        self._timeout_ms = timeout_ms
        if client is None:
            # Deferred, mirroring substrate.store.Store's pattern for its
            # Firestore client. Verified against the installed library:
            # unlike firestore.Client, genai.Client() does not resolve
            # credentials at construction time -- that happens lazily, on
            # the first real generate_content call -- so this deferral buys
            # no credential-avoidance genai.Client wouldn't already give for
            # free. It is kept anyway for the same reason store.py has it:
            # a caller that always injects a fake client -- every test in
            # this repo -- never runs this branch, or this import, at all.
            from google import genai

            # vertex_location, NOT location: `gemini-3.5-flash` is served
            # only from Vertex's `global` endpoint (see substrate/config.py).
            # config.location is us-central1 -- the Cloud Run/Firestore/
            # Pub/Sub/Jobs region -- and would 404 here in production while
            # every fake-backed test stayed green.
            client = genai.Client(
                vertexai=True,
                project=config.project_id,
                location=config.vertex_location,
            )
        self._client = client

    def generate(self, prompt: str, images: list[bytes] | None = None) -> str:
        from google.genai import types

        contents: list = [prompt]
        for image in images or []:
            mime_type = _sniff_mime_type(image)
            contents.append(types.Part.from_bytes(data=image, mime_type=mime_type))

        # Per-request http_options, not client-level, so the deadline applies
        # even when a client is injected. Checked in the installed library:
        # models.generate_content reads config.http_options and it takes
        # precedence over the client's global setting.
        response = self._client.models.generate_content(
            model=self._model,
            contents=contents,
            config=types.GenerateContentConfig(
                http_options=types.HttpOptions(timeout=self._timeout_ms)
            ),
        )
        if response.text is None:
            raise GeminiResponseEmpty(
                f"Vertex returned no text part for model {self._model!r}; "
                f"finish_reason={_finish_reason(response)!r}, "
                f"prompt_feedback={getattr(response, 'prompt_feedback', None)!r}"
            )
        if response.text == "":
            raise GeminiResponseEmpty(
                f"Vertex returned an empty text part for model {self._model!r}; "
                f"finish_reason={_finish_reason(response)!r}, "
                f"prompt_feedback={getattr(response, 'prompt_feedback', None)!r}"
            )
        return response.text
