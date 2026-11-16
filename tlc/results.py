"""Turning validated annotations into results: rows, the annotated image, the CSV.

Pure functions, no request handling. The view validates the payload and
computes each spot's Rf (:func:`tlc.views.rf_from_positions`); this module

* assigns every retained spot to the nearest lane marker (V-line, by |Δx|) and
  the nearest compound marker (H-line, by |Δy|) -- SI Eq. 10 and 11;
* orders the rows by lane label (natural order, unlabelled lanes last), then
  by Rf, and numbers them 1..n. The number is shown on the annotated image,
  in the table and in the CSV, so a crowded image stays readable;
* draws the annotated image: the baseline, solvent front and markers it was
  calculated from (SI 8.2), and each retained spot with its number, plus its
  Rf, compound and lane when full labels are requested;
* writes the CSV, with the same columns in the same order as the table, plus
  how each spot was placed and a block recording how the results were made
  (SI 10.3 / 11.4).
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any

import cv2
import numpy as np

# Marker types offered in the label popup (reviewer request); free text stays
# available alongside. Kept here so the server can echo them, and mirrored in
# tlc/static/tlc/js/annotations.js.
COMPOUND_TYPES = (
    "starting material",
    "expected product",
    "confirmed product",
    "by-product",
    "reagent",
)
LANE_TYPES = ("starting material", "product 1", "product 2", "by-product", "reagent")

MAX_LABEL_CHARS = 100

# BGR colours, matching the live canvas (tlc/static/tlc/js/canvas.js). Min
# and Max are yellow and magenta on a dark outline: the two differ clearly from
# each other and show on white, dark and green fluorescent plates alike (a
# green baseline nearly vanished on F254 plates under UV).
_YELLOW = (0, 214, 255)  # Min / baseline, #ffd600
_MAGENTA = (180, 0, 230)  # Max / solvent front, #e600b4
_DARK = (32, 32, 32)  # outline of Min and Max, #202020
_BLACK = (0, 0, 0)  # H-line / compound marker
_BLUE = (192, 101, 21)  # V-line / lane marker
_ORANGE = (0, 165, 255)  # spot ring
_WHITE = (255, 255, 255)


def clean_label(raw: Any) -> str:
    """A marker's text or type as a single trimmed line, or ``""``."""
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split())[:MAX_LABEL_CHARS]


def natural_key(text: str) -> list[tuple[int, int | str]]:
    """Sort key putting "Lane 2" before "Lane 10" (case-insensitive).

    Only ASCII digit runs count as numbers: ``str.isdigit`` also accepts "²"
    or "①", which ``int`` rejects. The split's captured runs are the odd parts.
    """
    return [
        (0, int(part)) if i % 2 else (1, part.casefold())
        for i, part in enumerate(re.split(r"([0-9]+)", text))
        if part
    ]


def _nearest(markers: list[dict[str, Any]], axis: str, value: float) -> dict[str, Any] | None:
    return min(markers, key=lambda m: abs(m[axis] - value), default=None)


# How a spot came to be, as recorded in the CSV.
SPOT_SOURCES = ("manual", "detected", "detected, moved")


def spot_source(spot: dict[str, Any]) -> str:
    """ "manual", "detected" (a Detect Spots candidate kept as proposed), or
    "detected, moved" (a candidate the user then dragged)."""
    if spot.get("auto") is True:
        return "detected, moved" if spot.get("moved") is True else "detected"
    return "manual"


def build_rows(spots: list[tuple[Any, ...]], markers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign context to each retained spot, sort, and number the rows.

    Args:
        spots: ``(x, y, rf)`` or ``(x, y, rf, source)`` per retained spot,
            original-image px; the source defaults to "manual".
        markers: Validated H/V lines, each ``{"type", "x"|"y", "text", "kind"}``.

    Returns:
        Rows ``{"n", "x", "y", "rf", "solvent", "solvent_type", "compound",
        "compound_type", "source"}``; ``solvent`` is the lane (kept under its historical
        key). An unlabelled lane is blank; a spot with no compound marker, or
        one carrying neither text nor type, is "Unknown" (decision D6).
    """
    lanes = [m for m in markers if m["type"] == "vertical"]
    compounds = [m for m in markers if m["type"] == "horizontal"]

    rows: list[dict[str, Any]] = []
    for x, y, rf, *rest in spots:
        lane = _nearest(lanes, "x", x)
        compound = _nearest(compounds, "y", y)
        compound_text = compound["text"] if compound else ""
        compound_type = compound["kind"] if compound else ""
        rows.append(
            {
                "x": x,
                "y": y,
                "rf": rf,
                "solvent": lane["text"] if lane else "",
                "solvent_type": lane["kind"] if lane else "",
                "compound": compound_text or ("" if compound_type else "Unknown"),
                "compound_type": compound_type,
                "source": rest[0] if rest else "manual",
            }
        )

    rows.sort(key=lambda r: (r["solvent"] == "", natural_key(r["solvent"]), r["rf"], r["x"]))
    for n, row in enumerate(rows, start=1):
        row["n"] = n
    return rows


# ---------------------------------------------------------------------------
# Annotated image
# ---------------------------------------------------------------------------


def _text(
    image: np.ndarray,
    text: str,
    org: tuple[int, int],
    scale: float,
    thickness: int,
    color: tuple[int, int, int] = _BLACK,
    halo_color: tuple[int, int, int] = _WHITE,
) -> None:
    """Draw ``text`` with a halo (white by default), so it reads on light and dark plates.

    The halo is a dilation of the glyph mask, not a second, thicker
    ``putText``: OpenCV 5 draws text at the same weight whatever the
    thickness, so a thick white copy was painted over completely. Work is
    confined to a window around the text, which matters on 50 MP plates.
    """
    font = cv2.FONT_HERSHEY_SIMPLEX
    (width, height), baseline = cv2.getTextSize(text, font, scale, thickness)
    halo = max(2, thickness)
    x0, y0 = org[0] - 2 * halo, org[1] - height - 2 * halo
    x1, y1 = org[0] + width + 2 * halo, org[1] + baseline + 2 * halo
    ih, iw = image.shape[:2]
    cx0, cy0, cx1, cy1 = max(0, x0), max(0, y0), min(iw, x1), min(ih, y1)
    if cx1 <= cx0 or cy1 <= cy0:
        return
    mask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    cv2.putText(mask, text, (org[0] - x0, org[1] - y0), font, scale, 255, thickness, cv2.LINE_AA)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * halo + 1, 2 * halo + 1))
    outline = cv2.dilate(mask, kernel)
    window = (slice(cy0 - y0, cy1 - y0), slice(cx0 - x0, cx1 - x0))
    region = image[cy0:cy1, cx0:cx1]
    region[outline[window] > 0] = halo_color
    alpha = (mask[window].astype(np.float32) / 255.0)[..., None]
    region[:] = (region * (1 - alpha) + np.array(color, np.float32) * alpha).astype(np.uint8)


def _line(
    image: np.ndarray,
    p1: tuple[int, int],
    p2: tuple[int, int],
    color: tuple[int, int, int],
    thickness: int,
    halo_color: tuple[int, int, int] = _WHITE,
) -> None:
    """A line on a thin halo (white by default), so it shows on any plate."""
    cv2.line(image, p1, p2, halo_color, thickness + 2)
    cv2.line(image, p1, p2, color, thickness)


def marker_caption(marker: dict[str, Any]) -> str:
    """How a marker is labelled on the image: "text (type)", either, or ""."""
    text, kind = marker.get("text", ""), marker.get("kind", "")
    if text and kind:
        return f"{text} ({kind})"
    return text or kind


def draw_annotations(
    image: np.ndarray,
    rows: list[dict[str, Any]],
    *,
    min_y: float,
    max_y: float,
    markers: list[dict[str, Any]],
    text_scale: float = 1.0,
    full_labels: bool = False,
) -> None:
    """Draw the analysis onto ``image`` (in place, full resolution).

    Reference lines and markers come first, so spot labels sit on top.
    Sizes scale with the image so a 3 MP and a 50 MP plate look alike;
    ``text_scale`` (the user's text-size setting) scales the text only.
    """
    text_scale = max(0.1, min(5.0, text_scale))
    height, width = image.shape[:2]
    s = max(1.0, min(height, width) / 1000.0)
    line_t = max(1, round(2 * s))
    radius = max(4, round(9 * s))
    font_scale = 0.55 * s * text_scale
    text_t = max(1, round(1.5 * s * text_scale))
    pad = round(6 * s)
    row_gap = round(22 * s * text_scale)

    for y, color, name in ((min_y, _YELLOW, "Min"), (max_y, _MAGENTA, "Max")):
        _line(image, (0, int(y)), (width, int(y)), color, line_t, _DARK)
        _text(image, name, (pad, int(y) - pad), font_scale, text_t, color, _DARK)

    # V-line captions sit along the bottom; neighbouring lanes are often closer
    # than a caption is wide, so a caption that would overlap the previous one
    # in its row moves up a row.
    row_ends: list[int] = []
    for marker in sorted(markers, key=lambda m: (m["type"], m.get("x", 0), m.get("y", 0))):
        caption = marker_caption(marker)
        if marker["type"] == "horizontal":
            y = int(marker["y"])
            _line(image, (0, y), (width, y), _BLACK, line_t)
            if caption:
                _text(image, caption, (pad, y - pad), font_scale, text_t)
            continue
        x = int(marker["x"])
        _line(image, (x, 0), (x, height), _BLUE, line_t)
        if not caption:
            continue
        caption_w = cv2.getTextSize(caption, cv2.FONT_HERSHEY_SIMPLEX, font_scale, text_t)[0][0]
        level = next((i for i, end in enumerate(row_ends) if x + pad > end), len(row_ends))
        if level == len(row_ends):
            row_ends.append(0)
        row_ends[level] = x + pad + caption_w + pad
        _text(image, caption, (x + pad, height - pad - level * row_gap), font_scale, text_t, _BLUE)

    for row in rows:
        x, y = int(row["x"]), int(row["y"])
        cv2.circle(image, (x, y), radius, _ORANGE, line_t)
        left = x + radius + pad
        # The number is the key into the table and CSV, so it is always drawn
        # and a little larger than the other text.
        _text(image, str(row["n"]), (left, y - pad), font_scale * 1.3, text_t + 1)
        if full_labels:
            details = [f"Rf {row['rf']:.2f}", row["compound"], row["solvent"]]
            for i, text in enumerate((t for t in details if t), start=1):
                _text(image, text, (left, y - pad + i * row_gap), font_scale, text_t)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

# Same columns, same order as the results table, then how each spot was
# placed (kept out of the compact table). Chemotion stores an uploaded
# CSV as a plain attachment and parses nothing in it (checked in
# chemotion_ELN: upload_attachment_from_third_party_app), so the layout is
# chosen for people. The column names, the CHMO line and the "Details" line
# are kept from version 1; the Details sentence matches how Rf values
# are written in Chemotion's free-text field, e.g. "0.53 (A), 0.50 (B)", so
# Rf has two decimals there and in the column, as in the table.
CSV_HEADER = [
    "spot",
    "mixture_label (X axis)",
    "mixture_type",
    "substance_label (Y axis)",
    "substance_type",
    "RF value",
    "spot source",
]


def _cell(value: Any) -> str:
    """A CSV cell that spreadsheets will not evaluate as a formula."""
    text = str(value)
    if text and text[0] in "=+-@\t\r":
        return "'" + text
    return text


def details_sentence(rows: list[dict[str, Any]], mobile_phase: str = "") -> str:
    """The Rf values as one sentence in the style of Chemotion's TLC free text.

    ``TLC (<mobile phase>); Rf (UV-Vis in nm, staining) = 0.53 (A)[h1], ...``;
    without a mobile phase, version 1's placeholder "solvents" stays.
    """
    inline = ", ".join(f"{row['rf']:.2f} ({row['compound']})[h{row['n']}]" for row in rows)
    return f"TLC ({mobile_phase or 'solvents'}); Rf (UV-Vis in nm, staining) = {inline}."


def csv_text(
    rows: list[dict[str, Any]],
    provenance: list[tuple[str, str]] | None = None,
    mobile_phase: str = "",
) -> str:
    """The results CSV, as text.

    ``provenance`` is written last as ``key;value`` lines, in the style of the
    CHMO line at the top. ``mobile_phase`` fills the Details sentence.
    """
    out = io.StringIO()
    out.write("Measurement type;thin layer chromatography (TLC);CHMO:0001007\n\n")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for row in rows:
        writer.writerow(
            [
                row["n"],
                _cell(row["solvent"]),
                _cell(row["solvent_type"]),
                _cell(row["compound"]),
                _cell(row["compound_type"]),
                f"{row['rf']:.2f}",  # as in the table: 0.50, not 0.5
                row.get("source", "manual"),
            ]
        )
    out.write(f"\nDetails:\n{details_sentence(rows, mobile_phase)}\n")
    if provenance:
        out.write("\nProvenance:\n")
        for key, value in provenance:
            out.write(f"{key};{_cell(value)}\n")
    return out.getvalue()
