"""Views for the TLC RF-calculator application.

The application lets a user annotate a Thin Layer Chromatography (TLC) plate
image in the browser and computes the retention factor (RF) for each marked
spot. Images arrive either from an Electronic Lab Notebook (ELN) via a remote
URL, or as a local upload whose bytes are sent base64-encoded in the request
body.

RF is the distance travelled by a spot divided by the distance travelled by
the solvent front, both measured from the baseline (SI Eq. 8)::

    rf = (y_baseline - y_spot) / (y_baseline - y_front)

Image y grows downwards, so the baseline (Min line) has the larger y.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import cv2
import numpy as np
import requests
from django.conf import settings
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from PIL import Image, ImageOps, UnidentifiedImageError
from requests.adapters import HTTPAdapter
from urllib3.exceptions import HTTPError as Urllib3Error

from config import __version__

from . import admission, detection, imagecache, media, results
from .inference import model_identities, models_loaded

logger = logging.getLogger(__name__)

# Limits applied when fetching a remote (ELN) image.
FETCH_TIMEOUT_SECONDS = 10  # per connect / read
# The whole download, so a server that drips bytes cannot hold a worker thread
# for long (25 MB in 60 s needs ~420 kB/s).
FETCH_DEADLINE_SECONDS = 60
MAX_IMAGE_BYTES = settings.MAX_IMAGE_MB * 1024 * 1024
MAX_REDIRECTS = 3  # ELN attachment URLs often redirect to storage
# Reject absurdly large images so a single request can't exhaust CPU/memory in
# the OpenCV paths (decoded to ~3 bytes/pixel).
MAX_IMAGE_PIXELS = settings.MAX_IMAGE_MEGAPIXELS * 1_000_000


def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness/readiness probe for container orchestration.

    Args:
        request: The incoming HTTP request.

    Returns:
        A small JSON body indicating the service is up, with the app version.
    """
    return JsonResponse({"status": "ok", "version": __version__})


@ensure_csrf_cookie
def upload_image(request: HttpRequest) -> HttpResponse:
    """Render the single-page annotation tool.

    Decorated with :func:`ensure_csrf_cookie` so the ``csrftoken`` cookie is
    present for the JavaScript ``fetch`` call to ``calculate_rf``.

    Args:
        request: The incoming HTTP request.

    Returns:
        The rendered annotation page.
    """
    return render(
        request,
        "tlc/annotate.html",
        {"version": __version__, "max_image_mb": settings.MAX_IMAGE_MB},
    )


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------


def _resolve_within_media(name: str) -> str | None:
    """Resolve a client-supplied name to an absolute path inside ``MEDIA_ROOT``.

    Only the final path component is used, so directory-traversal sequences
    (``..``) and absolute paths cannot escape the media directory.

    Args:
        name: The client-supplied file name.

    Returns:
        The absolute, canonicalised path, or ``None`` if it is empty or would
        resolve outside ``MEDIA_ROOT``.
    """
    safe_name = os.path.basename(name or "")
    if not safe_name or safe_name in (".", ".."):
        return None

    media_root = os.path.realpath(settings.MEDIA_ROOT)
    candidate = os.path.realpath(os.path.join(media_root, safe_name))
    if candidate == media_root or os.path.commonpath([media_root, candidate]) != media_root:
        return None
    return candidate


def _safe_address(url: str) -> str | None:
    """Resolve ``url``'s host and return the address to connect to (SSRF guard).

    The URL must use http(s) and must not resolve to a private, loopback,
    link-local, reserved, or otherwise non-public address. When
    ``settings.ELN_ALLOWED_HOSTS`` is non-empty (set directly or derived from
    ``ELN_ORIGIN``), the host must additionally appear in that allowlist.

    The fetch must connect to the returned address (:class:`_PinnedAddressAdapter`)
    rather than resolve the host again: a second lookup may answer differently
    (DNS rebinding).

    Args:
        url: The remote URL to validate.

    Returns:
        A validated IP address of the host, or ``None`` if the URL is not safe.
    """
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:  # e.g. a port out of range
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None

    allowed = [h.lower() for h in settings.ELN_ALLOWED_HOSTS]
    if allowed and parts.hostname.lower() not in allowed:
        return None

    try:
        addr_infos = socket.getaddrinfo(parts.hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror, UnicodeError:
        return None

    addresses = [str(info[4][0]) for info in addr_infos]
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return None
    # One address is pinned, with no fallback to the host's others, so prefer
    # IPv4: an IPv6 answer is of no use on a server without an IPv6 route.
    addresses.sort(key=lambda address: ":" in address)
    return addresses[0] if addresses else None


def _is_safe_remote_url(url: str) -> bool:
    """Return whether ``url`` is safe to fetch server-side (see :func:`_safe_address`)."""
    return _safe_address(url) is not None


class _PinnedAddressAdapter(HTTPAdapter):
    """Connects to one validated address instead of resolving the host again.

    The URL's host still goes in the ``Host`` header and is what TLS uses for
    SNI and certificate verification.
    """

    def __init__(self, address: str) -> None:
        self.address = address
        super().__init__()

    def build_connection_pool_key_attributes(
        self, request: requests.PreparedRequest, verify: Any, cert: Any = None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(
            request, verify, cert
        )
        pool_kwargs["server_hostname"] = host_params["host"]  # dropped for http
        host_params["host"] = self.address
        return host_params, pool_kwargs

    def send(
        self, request: requests.PreparedRequest, *args: Any, **kwargs: Any
    ) -> requests.Response:
        # Otherwise http.client would send the address as the Host header.
        request.headers["Host"] = urlsplit(request.url or "").netloc.rpartition("@")[2]
        return super().send(request, *args, **kwargs)


def _pinned_get(url: str, address: str) -> requests.Response:
    """Streamed GET of ``url`` with the connection made to ``address``."""
    session = requests.Session()
    adapter = _PinnedAddressAdapter(address)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session.get(url, timeout=FETCH_TIMEOUT_SECONDS, stream=True, allow_redirects=False)


def _fetch_following_safe_redirects(
    url: str, address: str | None = None
) -> requests.Response | None:
    """GET ``url``, following redirects while re-validating each hop for SSRF.

    Every hop, the first included, is resolved and validated by
    :func:`_safe_address` and fetched from the address that was validated, so
    an ELN endpoint that redirects to storage works without opening an SSRF hole.

    Args:
        url: The URL to fetch.
        address: ``_safe_address(url)`` if the caller already has it.

    Returns:
        The final non-redirect streamed response (the caller closes it), or
        ``None`` if a hop is unsafe, a redirect has no target, or the redirect
        limit is exceeded.
    """
    for _ in range(MAX_REDIRECTS + 1):
        if address is None:
            address = _safe_address(url)
            if address is None:
                logger.warning("Refused unsafe fetch target: %r", url)
                return None
        response = _pinned_get(url, address)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        response.close()
        if not location:
            return None
        url = urljoin(url, location)
        address = None
    return None


def _read_body(response: requests.Response, deadline: float) -> bytes | None:
    """The response body, or ``None`` if it exceeds ``MAX_IMAGE_BYTES``.

    Reads whatever has arrived (``read1``): ``iter_content`` waits for a whole
    chunk, so a server sending a byte at a time would get past the deadline.

    Raises:
        requests.Timeout: Past ``deadline`` (a ``time.monotonic`` value).
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := response.raw.read1(64 * 1024, decode_content=True):
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            return None
        chunks.append(chunk)
        if time.monotonic() > deadline:
            raise requests.Timeout("Remote image download took too long")
    return b"".join(chunks)


def _rotate_image(image: np.ndarray, rotation: int) -> np.ndarray:
    """Rotate ``image`` clockwise by ``rotation`` degrees (0/90/180/270)."""
    if rotation == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if rotation == 180:
        return cv2.rotate(image, cv2.ROTATE_180)
    if rotation == 270:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return image


MAX_FINE_ROTATION = 45.0


def _fine_rotate(image: np.ndarray, degrees: float) -> np.ndarray:
    """Straighten by ``degrees`` clockwise about the centre, keeping the size.

    Same width and height as the input: the photo's corners fall outside the
    frame and the uncovered corners are white. The browser draws the same
    turn about the canvas centre, so a point the user marks keeps its
    coordinates. ``cv2`` counts angles counter-clockwise, hence the sign, and
    indexes pixel centres, hence the centre at ``(w - 1) / 2``.
    """
    if degrees == 0:
        return image
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D(((width - 1) / 2, (height - 1) / 2), -degrees, 1.0)
    return cv2.warpAffine(
        image,
        matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(255, 255, 255),
    )


def _parse_fine_rotation(raw: Any) -> float:
    """The fine-tune angle (clockwise degrees, one decimal, within ±45), else 0."""
    value = _number(raw)
    if value is None or abs(value) > MAX_FINE_ROTATION:
        return 0.0
    return round(value, 1)


def _parse_rotation(raw: Any) -> int:
    """Return a valid clockwise rotation (0/90/180/270), else 0."""
    try:
        rotation = int(raw) % 360
    except TypeError, ValueError:
        return 0
    return rotation if rotation in (0, 90, 180, 270) else 0


@dataclass(frozen=True)
class _ImageSource:
    """A request's image, checked from its header only (nothing decoded yet)."""

    base_name: str
    width: int
    height: int
    raw: bytes | None = None  # sent inline (local image, fallback path)
    path: str | None = None  # stored in MEDIA_ROOT

    def job_bytes(self, kind: str, fine_rotation: bool = False) -> int:
        """Memory a ``kind`` of job ("lines", "spots", "render") needs on it;
        inline bytes are held while decoding."""
        return admission.job_bytes(
            self.width,
            self.height,
            kind=kind,
            cached=self.path is not None and imagecache.contains(self.path),
            fine_rotation=fine_rotation,
            load_models=not models_loaded(),
            extra=2 * len(self.raw) if self.raw is not None else 0,
        )

    def sha256(self) -> str:
        """SHA-256 of the image file as stored or sent (~30 ms for 13 MB)."""
        if self.raw is not None:
            return hashlib.sha256(self.raw).hexdigest()
        assert self.path is not None
        digest = hashlib.sha256()
        with open(self.path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def _inspect_image(raw: bytes | str, unreadable: str) -> tuple[str, int, int] | JsonResponse:
    """Format and size from an image's header, without decoding its pixels.

    Refuses files over ``MAX_IMAGE_BYTES`` and images over ``MAX_IMAGE_PIXELS``
    before any memory is spent on them.

    Args:
        raw: The image bytes, or a path to the image file.
        unreadable: The error message when it is not a readable image.
    """
    size = len(raw) if isinstance(raw, bytes) else os.path.getsize(raw)
    if size > MAX_IMAGE_BYTES:
        return JsonResponse({"error": "The image is too large."}, status=400)
    try:
        with Image.open(io.BytesIO(raw) if isinstance(raw, bytes) else raw) as pil_image:
            image_format = pil_image.format or ""
            width, height = pil_image.size
            pil_image.verify()
    except Image.DecompressionBombError:
        return JsonResponse({"error": "Image is too large to process."}, status=400)
    except UnidentifiedImageError, OSError, ValueError, SyntaxError:
        return JsonResponse({"error": unreadable}, status=400)
    if width * height > MAX_IMAGE_PIXELS:
        logger.warning("Refused oversized image: %dx%d", width, height)
        return JsonResponse({"error": "Image is too large to process."}, status=400)
    return image_format, width, height


def _open_image_source(
    data: dict[str, Any],
) -> tuple[_ImageSource | None, JsonResponse | None]:
    """Find and check the image an annotation payload refers to.

    A local image is referred to by ``imageSrc`` once uploaded (a media file
    name), or sent inline as a base64 ``imageData`` data URL if the upload
    failed; ELN images are always referred to by ``imageSrc``.

    Returns:
        ``(source, None)``, or ``(None, error)`` with a ready JsonResponse.
    """
    if data.get("isLocalImage") and data.get("imageData"):
        unreadable = "Could not decode the uploaded image."
        raw = _data_url_bytes(data["imageData"])
        if raw is None:
            return None, JsonResponse({"error": unreadable}, status=400)
        info = _inspect_image(raw, unreadable)
        if isinstance(info, JsonResponse):
            return None, info
        base_name = os.path.basename(data.get("imageSrc") or "local_upload.png")
        return _ImageSource(base_name, info[1], info[2], raw=raw), None

    image_src = data.get("imageSrc")
    if not image_src:
        return None, JsonResponse({"error": 'Missing "imageSrc" data in request'}, status=400)
    image_path = _resolve_within_media(image_src)
    if not image_path or not os.path.exists(image_path):
        logger.warning("Image not found for request: %r", image_src)
        return None, JsonResponse(
            {"error": "The image is no longer on the server. Please load it again."},
            status=400,
        )
    info = _inspect_image(image_path, "Failed to read the image file.")
    if isinstance(info, JsonResponse):
        return None, info
    media.touch(image_path)  # in use: keep it through the next sweep
    return _ImageSource(os.path.basename(image_src), info[1], info[2], path=image_path), None


def _load_image(source: _ImageSource, rotation: int, fine: float) -> np.ndarray | None:
    """Decode (or take from the cache) and rotate a checked image.

    ``rotation`` is the preset (0/90/180/270, exact); ``fine`` the deskew
    angle on top. The result may be read-only (a cached image); copy it
    before drawing.
    """
    if source.raw is not None:
        image = _decode_image_bytes(source.raw)
    else:
        assert source.path is not None
        image = imagecache.load(source.path, _decode_image_bytes)
    if image is None:
        return None
    return _fine_rotate(_rotate_image(image, rotation), fine)


def _busy() -> JsonResponse:
    response = JsonResponse(
        {"error": "The server is busy. Please try again in a moment."}, status=503
    )
    response["Retry-After"] = "10"
    return response


def _run_image_job(
    data: dict[str, Any],
    job: Callable[[np.ndarray, _ImageSource], JsonResponse],
    kind: str,
) -> JsonResponse:
    """Check the payload's image, wait for memory, load it, and run ``job``.

    ``job(image, source)`` does the work; it runs only once the memory the
    image needs is free (:mod:`tlc.admission`), else the client gets a 503.
    """
    source, error = _open_image_source(data)
    if error is not None:
        return error
    assert source is not None
    fine = _parse_fine_rotation(data.get("fineRotation"))

    def needed() -> int:  # re-estimated while waiting: the image may get cached
        return source.job_bytes(kind, fine_rotation=bool(fine))

    try:
        with admission.admit(needed, release=lambda: imagecache.release(keep=source.path)):
            image = _load_image(source, _parse_rotation(data.get("rotation")), fine)
            if image is None:
                message = (
                    "Could not decode the uploaded image."
                    if source.raw is not None
                    else "Failed to read the image file."
                )
                return JsonResponse({"error": message}, status=400)
            return job(image, source)
    except admission.Busy:
        logger.warning("Busy: refused a %d MB image job", needed() // 2**20)
        return _busy()


def _decode_data_url(data_url: str) -> np.ndarray | None:
    """Decode a base64 ``data:`` URL into an OpenCV image (BGR).

    EXIF orientation is applied so the decoded image matches what the browser
    displayed on the canvas (browsers auto-orient; ``cv2.imdecode`` does not),
    keeping annotation coordinates aligned for photos taken on phones.

    Args:
        data_url: A ``data:image/...;base64,<payload>`` string, or a bare
            base64 payload.

    Returns:
        The decoded BGR image as a NumPy array, or ``None`` if decoding fails.
    """
    raw = _data_url_bytes(data_url)
    return None if raw is None else _decode_image_bytes(raw)


def _data_url_bytes(data_url: str) -> bytes | None:
    """The bytes in a base64 ``data:`` URL (or bare payload), or ``None``."""
    if not isinstance(data_url, str):
        return None
    payload = data_url.split(",", 1)[1] if "," in data_url else data_url
    try:
        return base64.b64decode(payload, validate=True)
    except binascii.Error, ValueError:
        return None


def _decode_image_bytes(raw: bytes) -> np.ndarray | None:
    """Decode image bytes into an upright OpenCV image (BGR), or ``None``.

    The one decoder for every image the analysis sees -- local uploads and ELN
    images alike -- so both are oriented the way the browser shows them:
    phone photos are often stored sideways with an EXIF Orientation tag, which
    browsers apply and ``cv2`` applies only for some formats. Version 1 ignored
    the tag for local uploads, so it analysed such photos sideways (its ELN
    images, read with ``cv2.imread``, were oriented).
    """
    if len(raw) > MAX_IMAGE_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as pil_image:
            oriented = ImageOps.exif_transpose(pil_image).convert("RGB")
    except UnidentifiedImageError, OSError, ValueError:
        return None
    rgb = np.asarray(oriented)
    return np.ascontiguousarray(rgb[:, :, ::-1])  # RGB -> BGR for OpenCV


# ---------------------------------------------------------------------------
# RF calculation
# ---------------------------------------------------------------------------


def _number(raw: Any) -> float | None:
    """Return ``raw`` as a finite float, or ``None`` (bools are not numbers)."""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    return value if np.isfinite(value) else None


def _parse_markers(lines: list[Any]) -> list[dict[str, Any]] | None:
    """Validated H/V label lines, or ``None`` if a position is not a number.

    Each becomes ``{"type", "x"|"y", "text", "kind"}``: ``text`` is the free
    label and ``kind`` the type chosen from the popup's list.
    """
    markers = []
    for line in lines:
        if not isinstance(line, dict) or line.get("type") not in ("horizontal", "vertical"):
            continue
        axis = "y" if line["type"] == "horizontal" else "x"
        position = _number(line.get(axis))
        if position is None:
            return None
        markers.append(
            {
                "type": line["type"],
                axis: position,
                "text": results.clean_label(line.get("text")),
                "kind": results.clean_label(line.get("kind")),
            }
        )
    return markers


def rf_from_positions(*, y_baseline: float, y_front: float, y_spot: float) -> float | None:
    """Rf of a spot, or ``None`` if it lies outside the baseline-to-front interval.

    Implements SI Eq. 8, ``Rf = (y_baseline - y_spot) / (y_baseline - y_front)``,
    and keeps the spot when ``0 <= Rf <= 1`` (Eq. 9). The test is made on the
    unrounded value; rounding to two decimals is for reporting only, so a spot
    at Rf 0.996 is kept (reported as 1.0) and one exactly on the baseline is kept
    as Rf 0.0. Requires ``y_baseline > y_front``.
    """
    rf = (y_baseline - y_spot) / (y_baseline - y_front)
    if rf < 0.0 or rf > 1.0:
        return None
    return round(rf, 2)


def _parse_text_scale(raw: Any) -> float:
    """Return the annotation text-size multiplier, clamped to [0.1, 5.0].

    The frontend sends a fraction (e.g. ``1.0`` for 100%, ``2.5`` for 250%);
    anything missing or malformed falls back to 1.0. Clamped server-side so a
    crafted request cannot render absurdly large text.
    """
    try:
        scale = float(raw)
    except TypeError, ValueError:
        return 1.0
    return max(0.1, min(5.0, scale)) if np.isfinite(scale) else 1.0


def calculate_rf(request: HttpRequest) -> JsonResponse:
    """Compute RF values from annotation data and render an annotated image.

    Expects a JSON body containing ``lines`` (with ``min`` and ``max``
    reference lines plus optional ``vertical``/``horizontal`` label lines),
    ``spots``, and either ``imageSrc`` (a media file name, for ELN images) or
    ``imageData`` (a base64 data URL, for local uploads).

    Args:
        request: The HTTP request carrying the JSON annotation payload.

    Returns:
        A JSON response with ``rf_values``, ``annotated_image_url``, and
        ``csv_url``, or an ``error`` message with an appropriate status code.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request"}, status=405)

    try:
        data: dict[str, Any] = json.loads(request.body)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON format received")
        return JsonResponse({"error": "Invalid JSON format"}, status=400)

    if not isinstance(data, dict):
        return JsonResponse({"error": "Invalid JSON format"}, status=400)
    lines = data.get("lines")
    spots = data.get("spots")
    if not lines:
        return JsonResponse({"error": 'Missing "lines" data in request'}, status=400)
    if not spots:
        return JsonResponse({"error": 'Missing "spots" data in request'}, status=400)
    if not (
        isinstance(lines, list)
        and isinstance(spots, list)
        and all(isinstance(item, dict) for item in lines + spots)
    ):
        return JsonResponse({"error": "Lines and spots must be lists of objects."}, status=400)

    min_line = next((line for line in lines if line.get("type") == "min"), None)
    max_line = next((line for line in lines if line.get("type") == "max"), None)
    if not min_line or not max_line:
        return JsonResponse({"error": 'Please add "min" and "max" lines.'}, status=400)

    # Submitted coordinates must be numbers (SI 11.1).
    min_y = _number(min_line.get("y"))
    max_y = _number(max_line.get("y"))
    points = [(_number(spot.get("x")), _number(spot.get("y"))) for spot in spots]
    if min_y is None or max_y is None or any(x is None or y is None for x, y in points):
        return JsonResponse({"error": "Line and spot positions must be numbers."}, status=400)

    # Image y grows downwards, so the baseline (Min) must have the larger y
    # (SI 5.2, Eq. 4). This also refuses coincident lines, which would make the
    # solvent-front distance zero.
    if min_y <= max_y:
        return JsonResponse(
            {
                "error": "Min must be the baseline below the Max line (the solvent front). "
                "Swap or move the lines."
            },
            status=400,
        )

    markers = _parse_markers(lines)
    if markers is None:
        return JsonResponse({"error": "Line and spot positions must be numbers."}, status=400)

    retained = []
    for (x, y), spot in zip(points, spots, strict=True):
        assert x is not None and y is not None  # checked above
        rf_value = rf_from_positions(y_baseline=min_y, y_front=max_y, y_spot=y)
        if rf_value is not None:  # else below the baseline or above the front (SI 7.6)
            retained.append((x, y, rf_value, results.spot_source(spot)))
    rf_values = results.build_rows(retained, markers)

    def render(image: np.ndarray, source: _ImageSource) -> JsonResponse:
        return _render_results(image, source, data, rf_values, min_y, max_y, markers)

    return _run_image_job(data, render, "render")


def _rotation_text(data: dict[str, Any]) -> str:
    preset = _parse_rotation(data.get("rotation"))
    fine = _parse_fine_rotation(data.get("fineRotation"))
    if not fine:
        return f"{preset} degrees clockwise"
    return f"{preset} {'+' if fine > 0 else '-'} {abs(fine)} degrees clockwise"


def _provenance(source: _ImageSource, data: dict[str, Any]) -> list[tuple[str, str]]:
    """How a result was made, for the CSV (SI 10.3 / 11.4)."""
    region = _parse_region(data.get("plateRegion"))
    rows = [
        ("app", f"TLC RF Calculator {__version__}"),
        ("image", results.clean_label(data.get("imageName")) or source.base_name),
        ("image sha256", source.sha256()),
        ("rotation", _rotation_text(data)),
        ("plate area", "x={}, y={}, width={}, height={}".format(*region) if region else "none"),
        ("mobile phase", results.clean_label(data.get("mobilePhase")) or "not given"),
        ("stationary phase", results.clean_label(data.get("stationaryPhase")) or "not given"),
    ]
    for model in model_identities():
        rows.append((model["name"], f"{model['file']} sha256 {model['sha256']}"))
    rows.append(("calculated", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")))
    return rows


def _render_results(
    image: np.ndarray,
    source: _ImageSource,
    data: dict[str, Any],
    rf_values: list[dict[str, Any]],
    min_y: float,
    max_y: float,
    markers: list[dict[str, Any]],
) -> JsonResponse:
    """Draw the annotated image, write it and the CSV, and answer the request."""
    if not image.flags.writeable:  # a cached image is shared: draw on a copy
        image = image.copy()
    results.draw_annotations(
        image,
        rf_values,
        min_y=min_y,
        max_y=max_y,
        markers=markers,
        text_scale=_parse_text_scale(data.get("textScale")),
        full_labels=data.get("labelMode") == "full",
    )

    # Named by a random token alone: unique, so concurrent requests never
    # clobber each other, and safe in a path and a URL. The client's file name
    # (it may hold "#" or "%", or be too long) is recorded in the CSV instead.
    token = uuid.uuid4().hex
    annotated_name = f"annotated_{token}.png"
    csv_name = f"rf_values_{token}.csv"

    mobile_phase = results.clean_label(data.get("mobilePhase"))
    media.write(annotated_name, cv2.imencode(".png", image)[1].tobytes())
    media.write(csv_name, results.csv_text(rf_values, _provenance(source, data), mobile_phase))

    return JsonResponse(
        {
            "rf_values": rf_values,
            "annotated_image_url": settings.MEDIA_URL + annotated_name,
            "csv_url": settings.MEDIA_URL + csv_name,
            # The CSV's Details sentence, for "Copy Rf summary".
            "details": results.details_sentence(rf_values, mobile_phase),
        }
    )


# The files of one calculation share its random token (see _render_results).
_RESULT_CSV = re.compile(r"rf_values_([0-9a-f]{32})\.csv")
_RESULT_IMAGE = re.compile(r"annotated_([0-9a-f]{32})\.png")
MAX_ANNOTATIONS_CHARS = 1_000_000  # a file with thousands of spots is ~200 kB


def _export_stem(raw: Any) -> str:
    """The client's base name for exported files, safe in a zip and a header."""
    stem = re.sub(r"[^\w.-]+", "_", raw if isinstance(raw, str) else "").strip("._")[:120]
    return stem or "tlc_plate"


def download_results(request: HttpRequest) -> HttpResponse:
    """Stream one ZIP with a calculation's CSV, annotated image and annotations.

    Expects JSON ``{"csv", "image", "annotations", "name"}``: the CSV and image
    URLs (or names) from a :func:`calculate_rf` response, the annotation file
    as text (it lives in the browser), and the base name for the files in the
    archive. The CSV and image must be the two files of one calculation, so
    nothing else in ``MEDIA_ROOT`` can be read through this view.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request"}, status=405)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON format"}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({"error": "Invalid JSON format"}, status=400)

    def name_of(raw: Any) -> str:
        return os.path.basename(urlsplit(raw).path) if isinstance(raw, str) else ""

    csv_match = _RESULT_CSV.fullmatch(name_of(data.get("csv")))
    image_match = _RESULT_IMAGE.fullmatch(name_of(data.get("image")))
    if not (csv_match and image_match and csv_match[1] == image_match[1]):
        return JsonResponse({"error": "These are not the files of one result."}, status=400)

    annotations = data.get("annotations")
    if not isinstance(annotations, str) or len(annotations) > MAX_ANNOTATIONS_CHARS:
        annotations = ""
    try:
        parsed = json.loads(annotations)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict) or parsed.get("format") != "tlc-rf-annotations":
        return JsonResponse({"error": "The annotations are not a TLC annotation file."}, status=400)

    csv_path = _resolve_within_media(csv_match[0])
    image_path = _resolve_within_media(image_match[0])
    if not (csv_path and image_path and os.path.exists(csv_path) and os.path.exists(image_path)):
        return JsonResponse(
            {"error": "The results are no longer stored. Calculate RF again."}, status=404
        )

    stem = _export_stem(data.get("name"))
    # A 50 MP plate's PNG is tens of MB: build the archive in a temporary file,
    # not in memory, and stream it. The PNG is already compressed.
    archive = tempfile.TemporaryFile()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(csv_path, f"{stem}_rf.csv")
        zf.write(image_path, f"{stem}_annotated.png", compress_type=zipfile.ZIP_STORED)
        zf.writestr(f"{stem}_annotations.json", annotations)
    for path in (csv_path, image_path):
        media.touch(path)
    archive.seek(0)
    return FileResponse(
        archive, as_attachment=True, filename=f"{stem}.zip", content_type="application/zip"
    )


def _parse_region(raw: Any) -> tuple[int, int, int, int] | None:
    """Parse an optional ``{x, y, width, height}`` region into a tuple, or None."""
    if not isinstance(raw, dict):
        return None
    try:
        x, y = int(raw["x"]), int(raw["y"])
        width, height = int(raw["width"]), int(raw["height"])
    except KeyError, TypeError, ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return x, y, width, height


def _parse_detection_request(
    request: HttpRequest,
) -> tuple[dict[str, Any], JsonResponse | None]:
    """Shared front half of the detection views: method and JSON."""
    if request.method != "POST":
        return {}, JsonResponse({"error": "Invalid request"}, status=405)
    try:
        data: dict[str, Any] = json.loads(request.body)
    except json.JSONDecodeError:
        return {}, JsonResponse({"error": "Invalid JSON format"}, status=400)
    if not isinstance(data, dict):
        return {}, JsonResponse({"error": "Invalid JSON format"}, status=400)
    return data, None


def detect_spots(request: HttpRequest) -> JsonResponse:
    """Propose candidate spots with TLC-SpotDet, inside the Min/Max band.

    Accepts the same image payload as :func:`calculate_rf` plus ``minY`` and
    ``maxY`` (the baseline and solvent front, original-image px), which are
    required: inference is restricted to the band between them (SI section 4).
    An optional ``region`` (``{x, y, width, height}``, the Plate Area) also
    limits the x-range. Candidates are suggestions the user reviews.

    Args:
        request: The HTTP request carrying the JSON payload.

    Returns:
        ``{"spots": [{"x", "y", "conf", "w", "h"}], "count", "roi", "model"}``
        (``w``, ``h``: the box size), or an ``error``.
    """
    data, error = _parse_detection_request(request)
    if error is not None:
        return error

    try:
        min_y, max_y = float(data["minY"]), float(data["maxY"])
    except KeyError, TypeError, ValueError:
        return JsonResponse(
            {"error": "Place the Min and Max lines before detecting spots."}, status=400
        )
    region = _parse_region(data.get("region"))

    def detect(image: np.ndarray, _source: _ImageSource) -> JsonResponse:
        try:
            result = detection.detect_spots(image, min_y=min_y, max_y=max_y, region=region)
        except detection.DetectionError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("Spot detection failed")
            return JsonResponse({"error": "Spot detection failed."}, status=500)
        return JsonResponse({**result, "count": len(result["spots"])})

    return _run_image_job(data, detect, "spots")


def detect_lines(request: HttpRequest) -> JsonResponse:
    """Propose the baseline (Min) and solvent front (Max) with TLC-LineDet.

    Accepts the same image payload as :func:`calculate_rf`, plus an optional
    ``region`` (the Plate Area) to confine the search. The two lines are
    assigned by position, not by class label (see :mod:`tlc.detection`).

    Args:
        request: The HTTP request carrying the JSON image payload.

    Returns:
        ``{"minY", "maxY", "estimated", "model"}`` in original-image px, or an
        ``error``. ``estimated`` names a line that was guessed because only
        the other one was detected.
    """
    data, error = _parse_detection_request(request)
    if error is not None:
        return error
    region = _parse_region(data.get("region"))

    def detect(image: np.ndarray, _source: _ImageSource) -> JsonResponse:
        try:
            result = detection.detect_lines(image, region=region)
        except detection.DetectionError as exc:
            return JsonResponse({"error": str(exc)}, status=400)
        except Exception:
            logger.exception("Line detection failed")
            return JsonResponse({"error": "Line detection failed."}, status=500)
        return JsonResponse(result)

    return _run_image_job(data, detect, "lines")


# ---------------------------------------------------------------------------
# Remote image fetching / serving
# ---------------------------------------------------------------------------


def fetch_and_save_image(request: HttpRequest) -> JsonResponse:
    """Fetch an ELN image from a remote URL and store it under ``MEDIA_ROOT``.

    The URL is validated against SSRF (see :func:`_is_safe_remote_url`);
    redirects are followed but each hop is re-validated
    (:func:`_fetch_following_safe_redirects`). The download is bounded by a
    per-read timeout, an overall deadline and a maximum size, and the file is
    saved under a random name. Only that name (not a filesystem path) is
    returned, so the client cannot influence where subsequent reads occur.

    Args:
        request: The HTTP request; expects a ``url`` query parameter.

    Returns:
        A JSON response with ``imagePath`` (a media-relative file name), or an
        ``error`` message.
    """
    image_url = request.GET.get("url")
    if not image_url:
        return JsonResponse({"error": 'Missing "url" parameter'}, status=400)
    address = _safe_address(image_url)
    if address is None:
        logger.warning("Refused to fetch unsafe URL: %r", image_url)
        return JsonResponse({"error": "The provided URL is not allowed."}, status=400)

    failed = JsonResponse({"error": "Failed to fetch image"}, status=400)
    deadline = time.monotonic() + FETCH_DEADLINE_SECONDS
    try:
        response = _fetch_following_safe_redirects(image_url, address)
        if response is None:
            return failed
        try:
            if response.status_code != 200:
                return failed
            content = _read_body(response, deadline)
        finally:
            response.close()
    # Reading the body raises urllib3's errors (read1 bypasses requests' wrapping).
    except requests.RequestException, Urllib3Error:
        logger.warning("Error fetching remote image", exc_info=True)
        return failed
    if content is None:
        return JsonResponse({"error": "Remote image is too large."}, status=400)

    # Checked from the header, like a local upload: decoding a 50 MP photo
    # here just to validate it cost ~700 MB.
    info = _inspect_image(content, "Remote resource is not a valid image.")
    if isinstance(info, JsonResponse):
        return info
    extension = _UPLOAD_FORMATS.get(info[0])
    if extension is None:
        return JsonResponse({"error": "Remote resource is not a valid image."}, status=400)

    file_name = f"{uuid.uuid4().hex}{extension}"
    media.write(file_name, content)

    return JsonResponse({"imagePath": file_name})


# Formats a browser can show on the canvas, with the extension they are stored
# under (so /media/ serves them with the right type).
_UPLOAD_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "BMP": ".bmp", "GIF": ".gif"}


def upload_image_file(request: HttpRequest) -> JsonResponse:
    """Store a local image once, so later requests can refer to it by name.

    Without this, every Detect Lines / Detect Spots / Calculate RF request
    carried the whole image as base64 JSON -- ~17 MB each time for a 13 MB
    phone photo. The bytes are stored as uploaded; EXIF orientation is applied
    when they are read (:func:`_decode_image_bytes`), as for ELN images.

    Expects multipart ``image``. Checks the size cap, then the format and pixel
    cap from the image header, without decoding the pixels.

    Returns:
        ``{"imagePath": <media file name>}``, or an ``error``.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid request"}, status=405)
    upload = request.FILES.get("image")
    if upload is None:
        return JsonResponse({"error": 'Missing "image" file.'}, status=400)
    if upload.size is None or upload.size > MAX_IMAGE_BYTES:
        return JsonResponse({"error": "The image is too large."}, status=400)
    raw = upload.read()
    unsupported = "The file is not a supported image."
    info = _inspect_image(raw, unsupported)
    if isinstance(info, JsonResponse):
        return info
    extension = _UPLOAD_FORMATS.get(info[0])
    if extension is None:
        return JsonResponse({"error": unsupported}, status=400)

    file_name = f"{uuid.uuid4().hex}{extension}"
    media.write(file_name, raw)
    return JsonResponse({"imagePath": file_name})


def serve_image(request: HttpRequest) -> HttpResponse:
    """Serve an image stored under ``MEDIA_ROOT`` by name.

    The ``file_path`` parameter is confined to ``MEDIA_ROOT`` (see
    :func:`_resolve_within_media`), so arbitrary files on the host cannot be
    read.

    Args:
        request: The HTTP request; expects a ``file_path`` query parameter.

    Returns:
        The image bytes as ``image/png``, or a JSON ``error`` with status 404.
    """
    requested = request.GET.get("file_path")
    resolved = _resolve_within_media(requested or "")
    if not resolved or not os.path.exists(resolved):
        return JsonResponse({"error": "File not found"}, status=404)

    with open(resolved, "rb") as fh:
        return HttpResponse(fh.read(), content_type="image/png")
